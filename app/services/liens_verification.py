"""Décider si une adresse publiée mène encore quelque part (chantier K).

**Ce module ne parle à personne.** Il classe une réponse, il tient un état, et il
compare un domaine à une liste. La requête elle-même est passée en argument (K2) : c'est
ce qui permet d'éprouver toutes les règles ci-dessous sans réseau, donc sans dépendre
d'un site tiers ni d'une connexion.

Deux principes gouvernent tout le fichier :

  1. **Un faux positif est pire qu'un signalement manquant.** Dire qu'une source
     gouvernementale est morte alors qu'elle répond fait douter d'un chiffre juste, sur
     un site politique. Dans le doute, on se tait.
  2. **Une liste blanche dit d'où l'on part, pas où l'on arrive.** Elle ne remplace
     aucun des garde-fous du K2 (pas de redirection suivie, résolution contrôlée).
"""

import asyncio
import hashlib
import ipaddress
import logging
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Conversation,
    ConversationSource,
    ConversationState,
    ModerationStatus,
    Statement,
    StatementSource,
)
from app.models.lien import LienVerifie, Verdict

logger = logging.getLogger("app.liens")

#: Domaines dont on accepte d'interroger les pages. Arrêtée par le client le 5 septembre
#: 2026, et destinée à vivre : c'est une constante, pas une table — l'élargir coûte une
#: ligne de fichier, comme pour `THEMES`.
#:
#: **En ASCII, toujours.** Un domaine accentué est un IDN, qui ne s'écrit en DNS qu'en
#: punycode : « sénat.fr » dans cette liste ne correspondrait à rien. La comparaison se
#: fait sur la forme ASCII, comme l'affichage des domaines depuis le J1.
DOMAINES_AUTORISES = (
    "gouv.fr",
    "insee.fr",
    "ccomptes.fr",
    "service-public.fr",
    "data.gouv.fr",
    "assemblee-nationale.fr",
    "senat.fr",
    "europa.eu",
    "ameli.fr",
)

#: Il faut DEUX échecs pour signaler, et sept jours entre le premier et le dernier. Une
#: refonte de site un mardi matin ne doit pas faire crier au lien mort le mardi soir.
ECHECS_POUR_SIGNALER = 2
JOURS_POUR_SIGNALER = 7

#: Une adresse joignable n'est revue qu'au bout d'une semaine. Un lien ne meurt pas
#: souvent, et interroger un site public plus que nécessaire est impoli.
JOURS_ENTRE_DEUX_ESSAIS = 7

#: Adresses interrogées par passage du worker. C'est CE nombre qui borne la charge, et
#: non la taille du catalogue : à trois liens tout est vu en un passage, à trois cents
#: le tour se fait en quinze passages, soit moins de quatre heures. Un catalogue qui
#: grandit allonge le tour, il n'alourdit jamais un passage.
LIENS_PAR_PASSAGE = 20

#: Une seconde entre deux requêtes, et une seule à la fois. Ce n'est pas une précaution
#: technique — le worker n'est pas pressé — c'est de la politesse envers des serveurs
#: publics qui ne nous ont rien demandé.
PAUSE_ENTRE_REQUETES = 1.0

#: Délai d'une requête. Court : on ne demande qu'un en-tête, et une source qui met plus
#: de dix secondes à le rendre ne sera de toute façon pas classée « morte » — le délai
#: dépassé est un « je ne sais pas ».
DELAI_REQUETE = 10.0

#: L'agent nomme le site et donne où écrire. Un robot anonyme qui frappe à la porte d'un
#: service public est un robot qu'on finit par bloquer, et à juste titre.
AGENT = "RondPointBot/1.0 (+https://lerondpoint2027.fr)"

#: Les seuls codes qui disent « cette page a disparu ». Tout le reste est un « je ne
#: sais pas » : 403 et 429 disent que le site refuse les robots, 5xx qu'il va mal,
#: 405 que HEAD ne lui plaît pas. Aucun ne dit que la page est morte.
CODES_DISPARUS = (404, 410)


def domaine_de_hote(hote: str) -> str:
    """Le domaine sous sa forme comparable : ASCII, minuscule, sans point final."""
    hote = (hote or "").strip().rstrip(".").lower()
    if not hote:
        return ""
    try:
        return hote.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        # Un hôte que l'IDNA refuse n'est de toute façon pas joignable ; le rendre tel
        # quel le fera simplement tomber hors de la liste blanche.
        return hote


def domaine_autorise(hote: str, liste=DOMAINES_AUTORISES) -> bool:
    """Le domaine appartient-il à la liste blanche, lui ou l'un de ses sous-domaines ?

    **La comparaison porte sur les ÉTIQUETTES du nom, pas sur la chaîne.** C'est la
    faute classique de toute liste blanche écrite avec `endswith` : « gouv.fr » sans le
    point accepte `notgouv.fr`, et `xgouv.fr`. Le point le rend impossible, et l'égalité
    stricte couvre le domaine nu.

    Ce qui ne passe pas non plus, et qui est le vrai piège : `gouv.fr.attaquant.example`
    **finit** par `attaquant.example`, donc aucune entrée ne le reconnaît. C'est bien le
    suffixe qu'on teste, jamais le préfixe.
    """
    hote = domaine_de_hote(hote)
    if not hote:
        return False
    return any(hote == entree or hote.endswith("." + entree) for entree in liste)


def normaliser(url: str) -> str:
    """L'adresse sous la forme qui sert de clé.

    Le **fragment est retiré** : `#chapitre-3` désigne un endroit dans une page, pas une
    page — deux chiffres qui citent deux sections d'un même rapport interrogeraient
    sinon deux fois la même adresse.

    Le schéma et l'hôte passent en minuscules ; **le chemin et la requête, non**. Un
    chemin est sensible à la casse sur la plupart des serveurs, et « normaliser »
    `/Rapport.pdf` en `/rapport.pdf` fabriquerait un 404 qui n'existe pas.
    """
    morceaux = urlsplit((url or "").strip())
    return urlunsplit(
        (
            morceaux.scheme.lower(),
            domaine_de_hote(morceaux.hostname or "")
            + (f":{morceaux.port}" if morceaux.port else ""),
            morceaux.path,
            morceaux.query,
            "",
        )
    )


def empreinte_de(url: str) -> str:
    """SHA-256 hexadécimal de l'adresse normalisée : la clé d'unicité de la table.

    Voir `LienVerifie.empreinte` pour la raison — un index B-tree n'accepte pas une
    entrée de 8 000 octets.
    """
    return hashlib.sha256(normaliser(url).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Reponse:
    """Ce que le K2 rapportera d'une requête. Le code, ou rien du tout.

    `code` à None couvre indistinctement le délai dépassé, l'échec DNS et le refus de
    connexion : tous trois veulent dire « je n'ai pas pu demander », et aucun ne dit
    quelque chose sur la page.
    """

    code: int | None = None
    erreur: str | None = None


def classer(reponse: Reponse, hote: str) -> Verdict:
    """Le verdict d'une réponse. La fonction la plus importante du chantier.

    L'ordre des tests compte : un domaine hors liste n'aurait jamais dû être interrogé,
    et le dire avant de regarder le code évite de fonder un verdict sur une requête
    qui n'aurait pas dû partir.
    """
    if not domaine_autorise(hote):
        return Verdict.hors_perimetre
    if reponse.code is None:
        return Verdict.non_concluant
    if reponse.code in CODES_DISPARUS:
        return Verdict.injoignable
    if 200 <= reponse.code < 400:
        # 3xx compris : une redirection n'est pas une mort. On ne la SUIT pas (K2), mais
        # une page qui redirige est une page qui répond.
        return Verdict.joignable
    return Verdict.non_concluant


def appliquer(lien: LienVerifie, verdict: Verdict, code: int | None, maintenant: datetime) -> None:
    """Reporte un verdict sur l'état d'une adresse.

    La série d'échecs ne compte que les disparitions : un `non_concluant` **n'incrémente
    pas** le compteur et **ne le remet pas à zéro** non plus. Un site qui refuse les
    robots pendant trois semaines ne doit ni faire signaler ses pages, ni effacer la
    trace d'un 404 vu avant lui.
    """
    lien.verdict = verdict
    lien.code_http = code
    lien.dernier_essai = maintenant

    if verdict is Verdict.joignable:
        lien.dernier_succes = maintenant
        lien.echecs_consecutifs = 0
        lien.premier_echec = None
    elif verdict is Verdict.injoignable:
        lien.echecs_consecutifs += 1
        if lien.premier_echec is None:
            lien.premier_echec = maintenant


def signale(lien: LienVerifie, maintenant: datetime | None = None) -> bool:
    """Faut-il DIRE au public que ce lien est injoignable ?

    Trois conditions, et les trois sont nécessaires : le dernier verdict est une
    disparition, elle a été observée au moins deux fois, et la première remonte à au
    moins une semaine. C'est le seul endroit qui décide de ce qui s'affiche — le K3 s'y
    reportera plutôt que de refaire le calcul.
    """
    if lien.verdict is not Verdict.injoignable:
        return False
    if lien.echecs_consecutifs < ECHECS_POUR_SIGNALER:
        return False
    if lien.premier_echec is None:
        return False
    reference = maintenant or lien.dernier_essai
    if reference is None:
        return False
    return reference - lien.premier_echec >= timedelta(days=JOURS_POUR_SIGNALER)


async def verifier_une(client, lien: LienVerifie, maintenant: datetime) -> Verdict:
    """Interroge une adresse et reporte le verdict. **Le client est passé en argument.**

    C'est ce qui rend tout ce module éprouvable sans réseau : les tests fournissent un
    client factice qui rend 404, 403, 500 ou lève un délai dépassé, et vérifient les
    règles — la partie qui compte — sans dépendre d'un site tiers.

    Une adresse hors liste blanche n'est **pas interrogée du tout** : le verdict est
    posé sans qu'aucune requête ne parte. C'est la liste blanche qui décide de sortir,
    pas la réponse qui décide après coup.
    """
    hote = urlsplit(lien.url).hostname or ""
    if not domaine_autorise(hote):
        appliquer(lien, Verdict.hors_perimetre, None, maintenant)
        return Verdict.hors_perimetre

    try:
        reponse = Reponse(code=(await client.head(lien.url)).status_code)
    except Exception as erreur:  # noqa: BLE001 — tout échec de transport se vaut ici
        reponse = Reponse(code=None, erreur=type(erreur).__name__)

    verdict = classer(reponse, hote)
    appliquer(lien, verdict, reponse.code, maintenant)
    return verdict


# --- K2 : le recensement, la résolution sûre, et la passe du worker -----------------


def _adresses_publiees():
    """Les deux requêtes qui disent ce que le site PUBLIE, et rien d'autre.

    Un lien porté par une proposition non approuvée, ou par un débat en brouillon, n'est
    visible de personne. Aller l'interroger reviendrait à faire une requête sortante au
    nom d'un contenu que le site n'a pas publié — et à prévenir un site tiers qu'on
    s'apprête peut-être à le citer.
    """
    publiee = (
        Conversation.is_public.is_(True),
        Conversation.state != ConversationState.draft,
        Conversation.moderation_status == ModerationStatus.approved,
    )
    chiffres = (
        select(ConversationSource.url)
        .join(Conversation, Conversation.id == ConversationSource.conversation_id)
        .where(*publiee)
    )
    apports = (
        select(StatementSource.url)
        .join(Statement, Statement.id == StatementSource.statement_id)
        .join(Conversation, Conversation.id == Statement.conversation_id)
        .where(Statement.moderation_status == ModerationStatus.approved, *publiee)
    )
    return chiffres, apports


async def recenser(session: AsyncSession) -> int:
    """Inscrit les adresses publiées qui manquent à la table. Rend le nombre d'ajouts.

    Idempotent : l'empreinte porte l'unicité, et une adresse déjà connue n'est pas
    touchée — surtout pas son historique de vérification.
    """
    chiffres, apports = _adresses_publiees()
    urls = set(await session.scalars(chiffres)) | set(await session.scalars(apports))
    if not urls:
        return 0

    connues = set(await session.scalars(select(LienVerifie.empreinte)))
    ajouts = 0
    for url in urls:
        empreinte = empreinte_de(url)
        if empreinte in connues:
            continue
        connues.add(empreinte)  # deux adresses qui se normalisent pareil, même passage
        session.add(
            LienVerifie(
                empreinte=empreinte,
                url=normaliser(url),
                domaine=domaine_de_hote(urlsplit(url).hostname or ""),
            )
        )
        ajouts += 1
    if ajouts:
        await session.commit()
    return ajouts


async def a_revoir(session: AsyncSession, limite: int = LIENS_PAR_PASSAGE) -> list[LienVerifie]:
    """Les adresses à interroger maintenant : jamais vues d'abord, puis les plus vieilles.

    `nulls_first` n'est pas une coquetterie de tri : sans lui, PostgreSQL place les NULL
    en **dernier** en ordre croissant, et une adresse jamais vérifiée attendrait que
    toutes les autres aient vieilli de sept jours avant d'être regardée une première fois.
    """
    limite_temps = datetime.now(timezone.utc) - timedelta(days=JOURS_ENTRE_DEUX_ESSAIS)
    return list(
        await session.scalars(
            select(LienVerifie)
            .where(
                LienVerifie.dernier_essai.is_(None)
                | (LienVerifie.dernier_essai < limite_temps)
            )
            .order_by(LienVerifie.dernier_essai.asc().nulls_first(), LienVerifie.id)
            .limit(limite)
        )
    )


async def resoudre(hote: str) -> list[str]:
    """Les adresses IP d'un hôte. Liste vide si la résolution échoue.

    `getaddrinfo` bloque : il part dans un fil, sinon il fige la boucle d'événements du
    worker pendant tout le temps du DNS.
    """
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, hote, None, 0, socket.SOCK_STREAM
        )
    except OSError:
        return []
    return sorted({info[4][0] for info in infos})


def ip_publique(adresse: str) -> bool:
    """L'adresse est-elle sur l'Internet public ?

    `is_global` couvre la boucle locale, les plages privées, le lien-local
    (`169.254.0.0/16`, d'où sortent les métadonnées d'instance), le partage d'opérateur
    et les plages réservées. Les énumérer à la main aurait laissé passer celle qu'on
    oublie.

    **Mais il ne couvre PAS le multicast** : en Python 3.12, `224.0.0.1` et `ff02::1`
    ont `is_global` à vrai. C'est cohérent du point de vue de l'IANA — ces plages sont
    routables — et faux du nôtre : une requête vers un groupe multicast part sur le
    réseau local. D'où le second test, que le premier semblait rendre inutile.
    """
    try:
        ip = ipaddress.ip_address(adresse)
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


async def hote_sur(hote: str, resolveur=resoudre) -> bool:
    """L'hôte résout-il vers des adresses publiques, et seulement vers elles ?

    **Toutes** doivent l'être : un nom qui rend une adresse publique et une adresse
    interne ferait tomber la requête sur l'une ou l'autre selon l'humeur de la pile
    réseau, et il suffirait de recommencer pour atteindre l'interne.

    Le résolveur est un argument, pour que les tests éprouvent la règle sans DNS.
    """
    adresses = await resolveur(hote)
    return bool(adresses) and all(ip_publique(a) for a in adresses)


async def verifier_les_liens(
    session: AsyncSession,
    client,
    limite: int = LIENS_PAR_PASSAGE,
    pause: float = PAUSE_ENTRE_REQUETES,
    resolveur=resoudre,
) -> dict[str, int]:
    """Un passage : recense, choisit, interroge, enregistre. Rend le compte par verdict.

    **Deux contrôles avant toute requête**, dans cet ordre : la liste blanche, puis la
    résolution. Le premier dit qu'on a le droit de partir vers ce domaine ; le second,
    que ce domaine ne mène pas chez nous.

    Ce qui reste, et qu'il faut dire plutôt que de laisser croire à une étanchéité :
    entre la résolution et la connexion, le DNS peut changer d'avis — c'est le
    « rebinding ». La fenêtre est étroite, et surtout il faudrait pour l'exploiter
    maîtriser le DNS d'un domaine de la liste blanche, c'est-à-dire d'un service public.
    Ce que gagnerait alors l'attaquant est un code HTTP dans une colonne : aucun corps
    n'est lu, rien n'est renvoyé au visiteur qu'un « joignable » ou un « injoignable ».
    """
    await recenser(session)
    liens = await a_revoir(session, limite)
    comptes: dict[str, int] = {}
    maintenant = datetime.now(timezone.utc)

    for rang, lien in enumerate(liens):
        if rang and pause:
            await asyncio.sleep(pause)

        if not domaine_autorise(lien.domaine):
            appliquer(lien, Verdict.hors_perimetre, None, maintenant)
        elif not await hote_sur(lien.domaine, resolveur):
            # Ni joignable ni morte : on n'a pas demandé. Le dire « non concluant » est
            # exact, et évite qu'un domaine mal résolu soit un jour signalé au public.
            appliquer(lien, Verdict.non_concluant, None, maintenant)
            logger.warning("résolution refusée pour %s", lien.domaine)
        else:
            await verifier_une(client, lien, maintenant)

        comptes[lien.verdict.value] = comptes.get(lien.verdict.value, 0) + 1

    if liens:
        await session.commit()
    return comptes


# --- K3 : ce qui est DIT au public --------------------------------------------------
#
# Rien ici n'interroge quoi que ce soit : ce sont deux lectures de la table que le K2
# remplit. La décision — « faut-il le dire ? » — reste dans `signale()`, et ces deux
# fonctions s'y reportent au lieu de refaire le calcul à leur façon. C'est ce qui
# garantit qu'un lien signalé sur la page des chiffres l'est aussi sous la proposition
# et en modération, et qu'un seuil déplacé les déplace tous les trois ensemble.


async def signalements(session: AsyncSession, urls) -> dict[str, datetime]:
    """Parmi ces adresses, celles qu'il faut signaler — et depuis quand.

    Rend un dictionnaire **indexé par l'adresse telle qu'on l'a reçue**, et non par
    l'empreinte : l'appelant a des lignes à afficher, il ne doit pas avoir à normaliser
    lui-même pour retrouver son verdict. Deux citations d'une même page écrites
    différemment — une barre finale, un fragment — tombent sur la même ligne de la table
    et reçoivent donc la même date.

    Une seule requête pour toute une page, quel que soit le nombre de liens.
    """
    par_empreinte: dict[str, list[str]] = {}
    for url in urls:
        if url:
            par_empreinte.setdefault(empreinte_de(url), []).append(url)
    if not par_empreinte:
        return {}

    liens = await session.scalars(
        select(LienVerifie).where(
            LienVerifie.empreinte.in_(par_empreinte),
            # Ce filtre est un raccourci de lecture, pas la règle : `signale()` tranche
            # ensuite. Les deux doivent rester d'accord, et c'est pourquoi celui-ci ne
            # porte que sur le verdict — la condition la moins susceptible de bouger.
            LienVerifie.verdict == Verdict.injoignable,
        )
    )
    dates: dict[str, datetime] = {}
    for lien in liens:
        if not signale(lien):
            continue
        for url in par_empreinte[lien.empreinte]:
            dates[url] = lien.premier_echec
    return dates


@dataclass(frozen=True)
class Citation:
    """Un endroit du site où une adresse signalée est publiée.

    C'est ce qui manque à `LienVerifie` pour être actionnable. La table est indexée par
    adresse, or un modérateur ne corrige pas une adresse : il corrige un chiffre, dans
    un débat. Sans ce chemin de retour, l'écran dirait « quelque chose est mort quelque
    part ».
    """

    #: "chiffre" (saisi par un modérateur) ou "apport" (écrit par un participant). La
    #: distinction commande l'action : un chiffre se corrige, un apport se retire.
    genre: str
    conversation_id: int
    slug: str
    titre_debat: str
    libelle: str | None
    url: str


@dataclass(frozen=True)
class Signalement:
    """Un lien mort, et tous les endroits qui le citent."""

    lien: LienVerifie
    citations: tuple[Citation, ...]


async def _citations(session: AsyncSession, empreintes: set[str]) -> dict[str, list[Citation]]:
    """Où chacune de ces adresses est publiée, aujourd'hui.

    Mêmes conditions de publication qu'au recensement du K2, et pour la même raison : un
    lien porté par une proposition retirée depuis n'est plus affiché nulle part, donc il
    n'y a plus rien à corriger.
    """
    if not empreintes:
        return {}

    publiee = (
        Conversation.is_public.is_(True),
        Conversation.state != ConversationState.draft,
        Conversation.moderation_status == ModerationStatus.approved,
    )
    chiffres = (
        select(
            ConversationSource.url,
            ConversationSource.titre,
            Conversation.id,
            Conversation.slug,
            Conversation.title,
        )
        .join(Conversation, Conversation.id == ConversationSource.conversation_id)
        .where(*publiee)
    )
    apports = (
        select(
            StatementSource.url,
            StatementSource.label,
            Conversation.id,
            Conversation.slug,
            Conversation.title,
        )
        .join(Statement, Statement.id == StatementSource.statement_id)
        .join(Conversation, Conversation.id == Statement.conversation_id)
        .where(Statement.moderation_status == ModerationStatus.approved, *publiee)
    )

    trouvees: dict[str, list[Citation]] = {}
    for genre, requete in (("chiffre", chiffres), ("apport", apports)):
        for url, libelle, conversation_id, slug, titre in await session.execute(requete):
            empreinte = empreinte_de(url)
            if empreinte in empreintes:
                trouvees.setdefault(empreinte, []).append(
                    Citation(
                        genre=genre,
                        conversation_id=conversation_id,
                        slug=slug,
                        titre_debat=titre,
                        libelle=libelle,
                        url=url,
                    )
                )
    return trouvees


async def liens_a_corriger(session: AsyncSession) -> list[Signalement]:
    """Les liens signalés encore publiés, les plus anciennement morts d'abord.

    **Un lien qui n'est plus cité nulle part n'y figure pas**, même s'il reste dans la
    table : cet écran liste du travail à faire, pas un historique. Une adresse corrigée
    par un modérateur en disparaît donc à la correction, sans attendre un passage du
    worker — et l'ancienne ligne, elle, sera simplement cessée d'être interrogée.
    """
    candidats = [
        lien
        for lien in await session.scalars(
            select(LienVerifie)
            .where(LienVerifie.verdict == Verdict.injoignable)
            .order_by(LienVerifie.premier_echec.asc().nulls_last(), LienVerifie.id)
        )
        if signale(lien)
    ]
    citations = await _citations(session, {lien.empreinte for lien in candidats})
    return [
        Signalement(lien=lien, citations=tuple(citations[lien.empreinte]))
        for lien in candidats
        if lien.empreinte in citations
    ]


async def combien_a_corriger(session: AsyncSession) -> int:
    """Le nombre porté par la barre de modération. Voir `liens_a_corriger`."""
    return len(await liens_a_corriger(session))
