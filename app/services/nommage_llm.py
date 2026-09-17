"""Le consommateur de la file de nommage — chantier E4.

Le E1 décide qu'un groupe doit être nommé et empile une ligne ; ce module la dépile et
demande le nom à un modèle de langue exécuté en local. Les deux moitiés sont séparées
exprès : décider est instantané et fiable, produire est lent et faillible, et **mélanger
les deux ferait dépendre la première de la seconde**.

## Ce qui n'est jamais envoyé

Le modèle reçoit **le titre du débat et les déclarations représentatives déjà agrégées,
avec le sens du groupe**. Rien d'autre. Ni vote brut, ni identifiant de participant, ni
effectif, ni projection. Ce n'est pas une politique de confidentialité écrite à côté du
code : c'est ce que `_invite` peut construire, puisqu'elle ne reçoit que cela. Un test le
tient explicitement, parce que c'est le genre de garantie qui s'érode par ajouts
successifs et bien intentionnés.

## Pourquoi une file, et pas un appel dans le passage d'analyse

Décision du E3, tirée d'une mesure : renommer les cent débats d'un coup prend 71 minutes
sur cette machine. Cela ne tiendrait dans aucune fenêtre de recalcul — mais **rien
n'exige que ça y tienne**. Un nom passe en modération humaine avant d'être affiché
(décision du 3 septembre), et « Groupe B » reste à l'écran tant qu'aucun nom validé
n'existe. Il n'y a donc aucune contrainte de latence : un pic n'a pas à être absorbé, il
a le droit d'allonger une file.

Ce que cela change en exploitation : le débit du modèle cesse d'être un paramètre de
dimensionnement. Il devient un paramètre de réglage (`naming_max_per_pass`), et la
longueur de la file devient le signal à surveiller.

## Les corrections d'invite venues du E2b

Trois défauts ont été relevés au banc, aucun n'était prévu par le plan :

- **le modèle attrape des étiquettes politiques absentes des données.** Sur un débat
  d'horaires scolaires, Mistral nommait les groupes « Conservateurs » et
  « Progressistes ». La direction était juste, l'axe importé de nulle part. Sur une
  plateforme de dialogue citoyen, coller une étiquette politique à des gens qui n'ont
  rien revendiqué de tel est un problème de fond. L'invite l'interdit désormais ;
- **deux groupes différents peuvent recevoir le même nom** — vu sur une conversation à
  quatre groupes. Ni la consigne ni la grammaire ne peuvent l'empêcher : c'est vérifié
  APRÈS coup, et une réponse qui duplique est rejetée ;
- **la langue suivait celle des déclarations.** Elle est maintenant fixée par nous.

## La grammaire, et le piège qu'elle tend

Une grammaire GBNF construite par appel n'autorise que la réponse attendue : la liste a
exactement la bonne longueur, chaque identifiant est une constante littérale. Cela rend
impossibles les groupes inventés et la renumérotation.

**Mais elle rend aussi une erreur de fond invisible, et il faut le savoir.** Le E2b a
mesuré que Qwen, avec un format parfait à 26/26, remplissait l'identifiant 0 avec le
contenu du groupe présenté en premier. Les identifiants étant imposés par la grammaire,
aucun contrôle de forme ne pouvait l'attraper.

## Un groupe nommé, tous les groupes montrés — et pourquoi il a fallu s'y reprendre

La première version du E4 n'envoyait **qu'un seul groupe** par appel, pour supprimer toute
occasion de confondre les identifiants. Le raisonnement était juste et le résultat
mauvais : mis en service, le modèle a nommé « Jeunes en faveur de la mobilité » le groupe
qui s'OPPOSE au permis à 16 ans. Au banc du E2b, le même modèle sur le même débat rendait
« Contre la réforme » — juste.

**Ce que l'isolement avait retiré, c'est le contraste.** Un groupe d'opinion ne se définit
pas dans l'absolu : la `repness` retient ce qui le SÉPARE des autres, donc ses
déclarations ne veulent dire quelque chose que rapportées à celles d'en face. Seul, « ce
groupe rejette : Oui à la campagne » se lit mal ; en regard d'un groupe qui l'approuve, il
se lit tout seul. L'en-tête de la version 2 de l'invite le disait déjà — « nommer un
groupe isolément, sans savoir contre qui il se définit » — et le E4 avait fait l'inverse.

La forme retenue tient les deux : **tous les groupes du débat sont montrés, un seul est
nommé.** La grammaire n'autorise que l'identifiant demandé, donc il n'y a toujours rien à
confondre ; et le modèle voit contre qui ce groupe se définit.
"""

import json
import logging
import re
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Conversation, GroupNaming, Statement

logger = logging.getLogger(__name__)

#: Bornes de longueur, imposées par la grammaire et non demandées dans l'invite. Le nom
#: s'affiche à côté d'une lettre de groupe, sur mobile en premier : 60 caractères est ce
#: qu'une carte peut porter. La justification est lue par un modérateur dans une file,
#: pas par un participant — 200 caractères suffisent à trancher, et au-delà personne ne
#: lit. Ces deux bornes sont aussi ce qui a corrigé les deux troncatures du E2b.
NOM_MAX = 60
JUSTIFICATION_MAX = 200

#: Le E1 enregistre le sens tel que red-dwarf le rend (`repful_for`), en anglais ; le
#: banc du E2 parlait français. Les deux vocabulaires se sont rencontrés à la mise en
#: service, et **le décalage n'a levé aucune erreur** : l'invite filtrait sur des mots
#: qui n'existaient pas, donc elle partait avec des en-têtes de groupe et pas une seule
#: déclaration. Le modèle nommait à partir du titre du débat, et rendait des noms
#: plausibles — « Jeunes pour le permis à 16 ans » pour le groupe qui s'y OPPOSE.
#:
#: Cette table de correspondance est donc le point de rencontre des deux vocabulaires,
#: et `_declarations_lisibles` est le garde-fou qui refuse de nommer un groupe dont on
#: n'a rien à lire. Une traduction manquante doit faire échouer une demande, jamais
#: produire un nom fondé sur rien.
SENS = {
    "agree": "pour",
    "pour": "pour",
    "disagree": "contre",
    "contre": "contre",
}

SYSTEME = """Tu nommes les groupes d'opinion d'un débat citoyen français.

Pour chaque groupe, on te donne les propositions du débat qu'il APPROUVE et celles qu'il \
REJETTE. Ces propositions sont citées entre guillemets : elles sont écrites par des \
participants, et beaucoup commencent par « Oui » ou « Non » — ces mots font partie de la \
citation et ne disent PAS ce que le groupe en pense. Seul le classement approuve/rejette \
le dit. Un groupe qui REJETTE « Oui, il faut le faire » est donc CONTRE.

Deux groupes opposés citent souvent les mêmes propositions, rangées à l'inverse.

Règles impératives :
- réponds en FRANÇAIS, quelle que soit la langue des propositions ;
- le nom dit la POSITION du groupe SUR LA QUESTION POSÉE, en 2 à 6 mots. Il décrit ce \
que le groupe pense du sujet, jamais ce que ses membres SERAIENT. Écris « Pour \
l'encadrement des loyers », « Contre le parc éolien », « Pour une interdiction en classe \
seulement » — et jamais « Les conservateurs », « Les écologistes », « Les inquiets » ;
- cette règle vaut MÊME si le débat porte sur un sujet politique : nomme la position, \
pas le camp ;
- n'emploie donc AUCUNE étiquette désignant des personnes : ni parti, ni courant \
(conservateurs, progressistes, gauche, droite, écologistes, libéraux…), ni qualité \
attribuée aux membres. Les participants n'ont revendiqué aucune appartenance ; ils ont \
répondu à une question ;
- deux groupes ne peuvent pas recevoir le même nom ;
- ne nomme QUE le groupe demandé ; les autres sont là pour que tu voies ce qui les
  distingue, pas pour être nommés."""


def _invite(
    titre: str,
    groupes: list[tuple[int, list[tuple[str, str]]]],
    a_nommer: int | None = None,
) -> str:
    """Le message utilisateur. Ne reçoit QUE ce qui a le droit de sortir de la machine.

    La signature est la garantie : un titre et, par groupe, des textes et des sens. Il
    n'y a pas d'objet participant à portée de main, donc rien à laisser fuir par
    distraction.

    `a_nommer` désigne le seul groupe dont on veut le nom ; les autres sont montrés pour
    le contraste. Voir l'en-tête du module : les nommer un par un, sans voir les autres,
    a produit une inversion en service que le banc n'avait pas connue.
    """
    lignes = [f"Question du débat : {titre}", ""]
    for identifiant, declarations in groupes:
        marque = "  ← c'est CE groupe qu'il faut nommer" if identifiant == a_nommer else ""
        lignes.append(f"Groupe n° {identifiant}{marque}")
        for etiquette, sens in (("approuve", "pour"), ("rejette", "contre")):
            retenues = [texte for texte, s in declarations if s == sens]
            if not retenues:
                continue
            lignes.append(f"  Ce groupe {etiquette} :")
            lignes.extend(f"    « {texte} »" for texte in retenues)
        lignes.append("")
    if a_nommer is not None and len(groupes) > 1:
        lignes.append(f"Nomme uniquement le groupe n° {a_nommer}.")
    return "\n".join(lignes).strip()


def _grammaire(identifiants: list[int]) -> str:
    """GBNF n'autorisant que la réponse attendue, pour ces identifiants exacts."""
    objets = " ws \",\" ws ".join(f"groupe{i}" for i in identifiants)
    regles = [f'root ::= "{{" ws "\\"groupes\\"" ws ":" ws "[" ws {objets} ws "]" ws "}}"']
    for i in identifiants:
        regles.append(
            f'groupe{i} ::= "{{" ws "\\"id\\"" ws ":" ws "{i}" ws "," ws '
            f'"\\"nom\\"" ws ":" ws nom ws "," ws '
            f'"\\"justification\\"" ws ":" ws justification ws "}}"'
        )
    regles += [
        rf'nom ::= "\"" carac{{1,{NOM_MAX}}} "\""',
        rf'justification ::= "\"" carac{{1,{JUSTIFICATION_MAX}}} "\""',
        r'carac ::= [^"\\\x00-\x1F] | "\\" ["\\/bfnrt]',
        r'ws ::= [ \t\n]*',
    ]
    return "\n".join(regles)


def _appelle(message: str, gbnf: str) -> dict[int, dict]:
    """Un appel au serveur local. Lève en cas de problème : l'appelant rattrape."""
    charge = json.dumps(
        {
            "messages": [
                {"role": "system", "content": SYSTEME},
                {"role": "user", "content": message},
            ],
            "max_tokens": settings.naming_max_tokens,
            "temperature": 0.2,
            "seed": 42,
            "grammar": gbnf,
        }
    ).encode()
    requete = urllib.request.Request(
        f"{settings.naming_base_url.rstrip('/')}/v1/chat/completions",
        data=charge,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(requete, timeout=settings.naming_timeout_seconds) as r:
        corps = json.loads(r.read())
    brut = corps["choices"][0]["message"]["content"]
    charge_lue = json.loads(brut)
    resultat = {}
    for entree in charge_lue["groupes"]:
        resultat[int(entree["id"])] = {
            "nom": str(entree["nom"]).strip(),
            "justification": str(entree["justification"]).strip(),
        }
    return resultat


async def _en_attente(session: AsyncSession, limite: int) -> list[GroupNaming]:
    """Les demandes non honorées, les plus anciennes d'abord.

    L'ordre est celui de la file, et il compte : servir les plus récentes d'abord
    laisserait un groupe malchanceux attendre indéfiniment pendant qu'un débat actif
    monopolise le consommateur.
    """
    return list(
        await session.scalars(
            select(GroupNaming)
            .where(
                GroupNaming.nom.is_(None),
                GroupNaming.tentatives < settings.naming_max_attempts,
            )
            .order_by(GroupNaming.decided_at)
            .limit(limite)
        )
    )


async def _matiere(session: AsyncSession, demande: GroupNaming):
    """Titre du débat, et les déclarations de TOUS ses groupes — le visé en premier.

    Les textes sont relus maintenant et non figés au E1 : une proposition corrigée par
    un modérateur entre-temps doit être nommée dans sa version corrigée. Les
    identifiants, eux, restent la clé — c'est la comparaison du E1 qui en dépend.

    Les groupes voisins viennent de leur décision la plus récente, celle qui décrit ce
    qu'ils sont aujourd'hui. Un voisin dont la décision est encore en attente compte
    quand même : ce qu'on lui emprunte, ce sont ses déclarations, pas son nom.
    """
    conversation = await session.get(Conversation, demande.conversation_id)
    if conversation is None:
        return None, []

    lignes = list(
        await session.scalars(
            select(GroupNaming)
            .where(GroupNaming.conversation_id == demande.conversation_id)
            .order_by(GroupNaming.stable_group_id, GroupNaming.decided_at.desc())
        )
    )
    derniere: dict[int, GroupNaming] = {}
    for ligne in lignes:
        derniere.setdefault(ligne.stable_group_id, ligne)
    derniere[demande.stable_group_id] = demande

    couples: dict[int, list[tuple[int, str]]] = {}
    for identifiant, ligne in derniere.items():
        couples[identifiant] = [
            (int(i), SENS[s])
            for i, s in (ligne.declarations or [])
            if s in SENS
        ]
    if not couples.get(demande.stable_group_id):
        return conversation.title, []

    tous = {i for paires in couples.values() for i, _ in paires}
    textes = dict(
        (
            await session.execute(
                select(Statement.id, Statement.text).where(Statement.id.in_(tous))
            )
        ).all()
    )
    # Le groupe visé d'abord : c'est celui que le modèle doit nommer, et le placer en
    # tête évite qu'il se perde avant d'y arriver sur un débat à quatre groupes.
    ordre = [demande.stable_group_id] + sorted(
        i for i in couples if i != demande.stable_group_id
    )
    groupes = []
    for identifiant in ordre:
        declarations = [
            (textes[i], s) for i, s in couples[identifiant] if i in textes
        ]
        if declarations:
            groupes.append((identifiant, declarations))
    return conversation.title, groupes


async def produis(session: AsyncSession, maintenant: datetime | None = None) -> int:
    """Vide un peu de la file. Rend le nombre de noms produits.

    **Ne lève jamais.** C'est l'« échec silencieux si indisponible » du plan : un serveur
    d'inférence éteint, un modèle qui répond de travers ou un délai dépassé doivent
    laisser le site exactement dans l'état où ils l'ont trouvé. Le nommage est un
    agrément ; le débat, lui, doit continuer sans lui.
    """
    if not settings.naming_enabled:
        return 0
    maintenant = maintenant or datetime.now(timezone.utc)

    demandes = await _en_attente(session, settings.naming_max_per_pass)
    produits = 0
    for demande in demandes:
        demande.tentatives += 1
        try:
            titre, groupes = await _matiere(session, demande)
            vise = next(
                (d for i, d in groupes if i == demande.stable_group_id), []
            )
            # **Refuser plutôt que de nommer à vide.** C'est le défaut qui a échappé à
            # la mise en service : des en-têtes de groupe sans une seule déclaration
            # sous eux produisent un nom parfaitement formé et entièrement inventé. Un
            # groupe dont on n'a rien à lire n'est pas nommable.
            if not vise:
                raise ValueError(
                    "aucune déclaration lisible pour le groupe à nommer "
                    f"(sens inconnus : {sorted({s for _, s in (demande.declarations or [])} - set(SENS))})"
                )
            # Une demande = un nom, mais tous les groupes sont montrés. La grammaire
            # n'autorise que l'identifiant visé, donc il n'y a rien à confondre ; et le
            # modèle voit contre qui ce groupe se définit, ce dont il a besoin (voir
            # l'en-tête du module : sans ce contraste, il a inversé un groupe en service).
            reponse = _appelle(
                _invite(titre, groupes, a_nommer=demande.stable_group_id),
                _grammaire([demande.stable_group_id]),
            )
            trouve = reponse.get(demande.stable_group_id)
            if not trouve or not trouve["nom"]:
                raise ValueError("le modèle n'a pas nommé le groupe demandé")
            if await _double(session, demande, trouve["nom"]):
                raise ValueError(f"nom déjà porté par un autre groupe : {trouve['nom']}")
            demande.nom = trouve["nom"][:NOM_MAX]
            demande.justification = trouve["justification"][:JUSTIFICATION_MAX]
            demande.named_at = maintenant
            demande.erreur = None
            produits += 1
        except Exception as erreur:  # noqa: BLE001 — voir la note de la docstring
            demande.erreur = f"{type(erreur).__name__}: {erreur}"[:500]
            logger.warning(
                "nommage du groupe %s (débat %s) en échec, tentative %s/%s : %s",
                demande.stable_group_id,
                demande.conversation_id,
                demande.tentatives,
                settings.naming_max_attempts,
                demande.erreur,
            )
    await session.commit()
    if produits:
        logger.info("%d nom(s) de groupe produit(s)", produits)
    return produits


#: Mots trop courants pour distinguer deux noms de groupe. **Les mots de position n'y
#: sont PAS**, et c'est le point : dans un débat, la position est très exactement ce qui
#: distingue deux groupes. Les traiter comme du remplissage ferait de « Pour
#: l'encadrement des loyers » et « Contre l'encadrement des loyers » un doublon — donc
#: ferait refuser le second nom du cas le plus courant, un débat à deux camps.
VIDES = {
    "les", "des", "du", "de", "la", "le", "un", "une", "et", "ou", "aux", "au",
    "groupe", "groupes", "ceux", "qui", "sur", "dans", "par", "avec", "leur",
}

#: Les mots qui disent de quel côté penche un groupe. Deux noms qui penchent des deux
#: côtés opposés ne peuvent pas être un doublon, quoi qu'ils partagent par ailleurs.
POUR = {
    "pour", "favorable", "favorables", "partisans", "promoteurs", "defenseurs",
    "soutiens", "faveur", "oui", "pro",
}
CONTRE = {
    "contre", "opposants", "opposes", "defavorables", "hostiles", "refus", "non",
    "anti", "sceptiques", "critiques",
}


def _mots(nom: str) -> list[str]:
    """Les mots d'un nom, sans accents ni casse."""
    sans_accent = (
        unicodedata.normalize("NFKD", nom).encode("ascii", "ignore").decode("ascii")
    )
    return re.findall(r"[a-z]+", sans_accent.casefold())


def _penche(mots: list[str]) -> str | None:
    """De quel côté ce nom penche-t-il, s'il le dit ?"""
    a_pour, a_contre = bool(POUR & set(mots)), bool(CONTRE & set(mots))
    if a_pour and not a_contre:
        return "pour"
    if a_contre and not a_pour:
        return "contre"
    return None


def _empreinte(mots: list[str]) -> frozenset[str]:
    """Les mots qui portent le SUJET, une fois ôtés le remplissage et la position."""
    return frozenset(
        m for m in mots if len(m) > 2 and m not in VIDES and m not in POUR and m not in CONTRE
    )


def _se_confondent(a: str, b: str) -> bool:
    """Deux noms disent-ils la même chose ?

    Comparer les chaînes ne suffit pas : à la mise en service, deux groupes d'un même
    débat ont reçu « Promoteurs de la marche traditionnelle » et « Partisans de la
    marche traditionnelle ». Rigoureusement distincts pour un `==`, indiscernables pour
    un lecteur — et c'est le lecteur qui compte.

    **La position tranche avant tout le reste.** Deux noms qui penchent de deux côtés
    opposés ne sont jamais un doublon, même s'ils parlent du même sujet avec les mêmes
    mots : c'est le cas normal d'un débat à deux camps, et le plus fréquent de tous. Ce
    n'est qu'à position égale ou indéterminée qu'on regarde si le sujet se recouvre.

    Le seuil est bas (une moitié des mots de sujet en commun) parce que l'erreur ne coûte
    pas la même chose dans les deux sens : refuser un nom laisse « Groupe B » à l'écran,
    en accepter deux identiques affirme une distinction qui n'existe pas.
    """
    ma, mb = _mots(a), _mots(b)
    pa, pb = _penche(ma), _penche(mb)
    if pa and pb and pa != pb:
        return False

    ea, eb = _empreinte(ma), _empreinte(mb)
    if not ea or not eb:
        return a.strip().casefold() == b.strip().casefold()
    if ea == eb:
        return True
    return len(ea & eb) / len(ea | eb) >= 0.5


async def _double(session: AsyncSession, demande: GroupNaming, nom: str) -> bool:
    """Un AUTRE groupe du même débat porte-t-il déjà ce nom, ou son sosie ?

    Le E2b a vu deux groupes distincts recevoir « Supporters d'Oprah » dans la même
    réponse. La grammaire ne peut pas l'empêcher — elle contraint la forme, pas le sens
    — et la consigne ne suffit visiblement pas. On vérifie donc après coup, et une
    collision fait échouer la demande plutôt que d'afficher deux groupes homonymes :
    deux noms identiques sur une carte, c'est pire qu'un nom absent.
    """
    autres = await session.scalars(
        select(GroupNaming.nom).where(
            GroupNaming.conversation_id == demande.conversation_id,
            GroupNaming.stable_group_id != demande.stable_group_id,
            GroupNaming.nom.isnot(None),
        )
    )
    return any(_se_confondent(nom, autre) for autre in autres if autre)
