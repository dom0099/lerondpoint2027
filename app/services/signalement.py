"""La liste fermée des motifs de signalement, leur routage, et les messages à l'auteur.

Un **signalement** est ce qu'émet un lecteur sur une proposition déjà publiée. Il n'est
pas un verdict : c'est une plainte, elle ouvre un examen et ne le conclut pas. Un seul
cas fait exception — la **ligne rouge**, où le premier signalement suffit à déclencher
un **retrait conservatoire**, sans seuil ni quorum, parce que le coût d'attendre y est
plus élevé que le coût de se tromper. Un retrait conservatoire s'annule en un clic ;
une diffusion ne s'annule pas.

**Pourquoi une constante et non une table**, comme `app/services/themes.py` et pour les
mêmes motifs : une liste de dix cases qui bougera deux fois par an ne vaut pas un écran
d'administration à écrire, à protéger et à tester, et une taxonomie ouverte dérive. Ce
qui va en base est le **code court** ; le libellé n'existe qu'à l'affichage et peut être
réécrit sans migration.

**Ce module ne touche pas la base.** Le dépôt d'un signalement, le retrait et la file du
responsable sont dans `app/services/signalement_file.py`. Ici : la liste, la règle, les
gabarits de message — de quoi juger le dispositif d'un coup d'œil, sans lire de SQL.

Vocabulaire figé (§2 de la consigne MOD-3a), à respecter dans le code, les URL et les
messages : **signalement** (jamais « report » ni « flag »), **motif** (jamais « raison »
ni « catégorie »), **ligne rouge** (jamais « grave »), **retrait conservatoire** (jamais
« suppression »), **responsable** (jamais « modérateur »). Et si une habilitation
apparaît un jour, elle se nomme `habilitation` — « niveau » désigne déjà la jauge de jeu
du C5.
"""

import enum
import hashlib
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.config import settings


class Famille(str, enum.Enum):
    """Le regroupement des motifs. Sert à lire la liste, jamais à décider seul.

    C'est la ROUTE qui décide (voir `router`) ; la famille dit seulement de quelle
    nature est le reproche. Les deux coïncident aujourd'hui pour `ligne_rouge`, et
    c'est un hasard qu'il ne faut pas transformer en règle : `fausse_information` et
    `source_douteuse` partagent une famille et une route, `publicite_spam` et `autre`
    partagent une route sans partager de famille.
    """

    ligne_rouge = "ligne_rouge"
    qualite_fait = "qualite_fait"
    qualite_forme = "qualite_forme"
    volume = "volume"
    non_classable = "non_classable"


class Route(str, enum.Enum):
    """Ce qu'un signalement déclenche.

    **Dans ce lot, seule `RETRAIT_CONSERVATOIRE` a un effet.** Les quatre autres posent
    une ligne dans la file du responsable avec leur étiquette, et rien d'autre. C'est ce
    qui rend le lot réversible : rien n'attend un seuil qui n'existe pas, rien ne
    reformule, rien n'envoie de courriel.
    """

    RETRAIT_CONSERVATOIRE = "RETRAIT_CONSERVATOIRE"
    A_QUALIFIER = "A_QUALIFIER"
    A_REFORMULER = "A_REFORMULER"
    A_FUSIONNER = "A_FUSIONNER"
    FILE_RESPONSABLE = "FILE_RESPONSABLE"


@dataclass(frozen=True)
class Motif:
    code: str
    #: Ce que lit le signaleur sur la case à cocher.
    libelle: str
    famille: Famille
    route: Route
    #: Le fragment qui se glisse dans « Votre proposition a été retirée car {motif} ».
    #: Un libellé de case à cocher ne s'y insère pas — « retirée car Propos haineux /
    #: humiliant » n'est pas une phrase française, et l'exposé des motifs dû par le DSA
    #: est adressé à quelqu'un, pas rempli par un gabarit.
    phrase: str


#: Les dix motifs, **pas un de plus**, dans l'ordre d'affichage arrêté par le client.
#:
#: L'ordre n'est ni l'alphabet ni une fréquence : il va du plus grave au moins grave, et
#: c'est ce qui le rend exploitable par `router` — la route retenue est celle du motif
#: le mieux placé dans cette liste. Réordonner cette constante change donc la règle.
MOTIFS: tuple[Motif, ...] = (
    Motif(
        "propos_haineux",
        "Propos haineux / humiliant",
        Famille.ligne_rouge,
        Route.RETRAIT_CONSERVATOIRE,
        "elle a été signalée comme contenant des propos haineux ou humiliants",
    ),
    Motif(
        "incitation_violence",
        "Incitation à la violence",
        Famille.ligne_rouge,
        Route.RETRAIT_CONSERVATOIRE,
        "elle a été signalée comme incitant à la violence",
    ),
    Motif(
        "discrimination",
        "Discrimination",
        Famille.ligne_rouge,
        Route.RETRAIT_CONSERVATOIRE,
        "elle a été signalée comme discriminatoire",
    ),
    Motif(
        "donnees_personnelles",
        "Données personnelles d'un tiers (nom, coordonnées…)",
        Famille.ligne_rouge,
        Route.RETRAIT_CONSERVATOIRE,
        "elle fait apparaître les données personnelles d'un tiers",
    ),
    Motif(
        "fausse_information",
        "Fausse information",
        Famille.qualite_fait,
        Route.A_QUALIFIER,
        "elle a été signalée comme contenant une information fausse",
    ),
    Motif(
        "source_douteuse",
        "Source douteuse",
        Famille.qualite_fait,
        Route.A_QUALIFIER,
        "la source qu'elle avance a été signalée comme douteuse",
    ),
    Motif(
        "mal_formule",
        "Mal formulé / hors sujet",
        Famille.qualite_forme,
        Route.A_REFORMULER,
        "elle a été signalée comme mal formulée ou hors sujet",
    ),
    Motif(
        "doublon",
        "Doublon d'une proposition déjà existante",
        Famille.qualite_forme,
        Route.A_FUSIONNER,
        "elle reprend une proposition déjà présente dans ce débat",
    ),
    Motif(
        "publicite_spam",
        "Publicité / spam",
        Famille.volume,
        Route.FILE_RESPONSABLE,
        "elle a été signalée comme publicitaire",
    ),
    Motif(
        "autre",
        "Autre raison",
        Famille.non_classable,
        Route.FILE_RESPONSABLE,
        "elle a été signalée",
    ),
)

#: Le code du seul motif qui accepte — et exige — un texte libre.
MOTIF_TEXTE_LIBRE = "autre"
#: Plafond du texte libre. Au-delà, refus, jamais de troncature silencieuse.
TEXTE_LIBRE_MAX = 500
#: Deux motifs au plus par signalement. Le plafond est aussi porté par la BASE : deux
#: colonnes `motif_1`/`motif_2` plutôt qu'un JSON, ce qui le rend structurel.
MOTIFS_MAX = 2

_PAR_CODE: dict[str, Motif] = {m.code: m for m in MOTIFS}
#: Rang d'affichage, du plus grave au moins grave. Voir `router`.
_RANG: dict[str, int] = {m.code: rang for rang, m in enumerate(MOTIFS)}

CODES: tuple[str, ...] = tuple(m.code for m in MOTIFS)


class SignalementInvalide(ValueError):
    """Un dépôt refusé. Porte une phrase française destinée à être affichée telle quelle."""


def motif(code: str) -> Motif:
    """Le motif de ce code. Lève `SignalementInvalide` si le code est inconnu.

    Lève plutôt que de filtrer, contrairement à `themes_connus` : un thème retiré de sa
    liste doit cesser d'être proposé sans casser les débats qui le portent, alors qu'un
    motif inconnu au dépôt est une requête fabriquée à la main — et un signalement qu'on
    accepterait en ignorant son motif serait un signalement sans objet.
    """
    try:
        return _PAR_CODE[code]
    except KeyError:
        raise SignalementInvalide(f"Motif inconnu : {code!r}.") from None


def decidant(motifs: list[str]) -> Motif:
    """Celui des motifs qui **décide**, c'est-à-dire le plus grave des cochés.

    Trois endroits en avaient besoin et le recalculaient chacun de leur côté : la route
    (`router`), le message à l'auteur, et depuis le MOD-15 le rappel. Le rang vient de
    l'ordre de `MOTIFS`, qui est la seule table de gravité du chantier ; une seconde
    façon de trier finirait par dire qu'un doublon est plus grave qu'une incitation à la
    violence dans un écran et pas dans l'autre.

    Lève `SignalementInvalide` sur un code inconnu, comme `motif`.
    """
    return min((motif(code) for code in motifs), key=lambda m: _RANG[m.code])


def libelle(code: str) -> str:
    """Le libellé à afficher. Rend le code lui-même si la liste ne le connaît plus.

    Filtre ici, là où `motif` lève : à l'affichage, un motif retiré de la liste doit
    rester lisible dans la file et dans le rapport — les signalements déjà déposés sous
    ce code existent, et les faire disparaître de l'écran serait pire que les montrer
    sous leur code.
    """
    connu = _PAR_CODE.get(code)
    return connu.libelle if connu else code


def router(motifs: list[str]) -> Route:
    """La route d'un signalement portant ces motifs. **Pure, déterministe, sans E/S.**

    Contrat, tenu strictement :

      - **un motif au moins, deux au plus.** Zéro ou trois et plus : refus. Jamais de
        troncature silencieuse — tronquer choisirait à la place du signaleur, et le
        choix tombé serait invisible ;
      - **la gravité l'emporte sur la fréquence.** Si l'un des deux motifs est de
        famille `ligne_rouge`, la route est `RETRAIT_CONSERVATOIRE`, quelle que soit
        l'autre case. C'est la règle « en cas de doute, l'item monte, il ne descend
        pas ». La fréquence servira plus tard, et seulement à choisir le *message* —
        jamais la *décision*.

    La règle générale est celle-ci : **la route retenue est celle du motif le mieux
    placé dans `MOTIFS`**, qui est ordonnée du plus grave au moins grave. Elle englobe
    le cas de la ligne rouge (ses quatre motifs ouvrent la liste) et tranche aussi les
    paires que la consigne ne nomme pas — `fausse_information` + `doublon` part en
    `A_QUALIFIER`, et non en `A_FUSIONNER`. Une paire sans règle serait tranchée par
    l'ordre de saisie, c'est-à-dire par le hasard.

    Deux conséquences délibérées, qui ne se déduisent pas de l'ordre et qu'il faut lire
    comme des décisions :

      - **`autre` n'emporte aucun retrait, jamais, même seul.** Une case libre qui
        supprime est la case que choisira quiconque veut faire tomber une proposition
        sans avoir à la justifier. Elle monte vite dans la file, elle ne retire rien ;
      - **`fausse_information` non plus.** Sur ce site, une affirmation fausse mais
        inoffensive se qualifie, elle ne se supprime pas.
    """
    if not motifs:
        raise SignalementInvalide("Un signalement demande au moins un motif.")
    if len(motifs) > MOTIFS_MAX:
        raise SignalementInvalide(
            f"Un signalement porte {MOTIFS_MAX} motifs au plus, "
            f"et celui-ci en porte {len(motifs)}."
        )
    if len(set(motifs)) != len(motifs):
        # Deux fois la même case n'est pas deux motifs. Dédupliquer en silence
        # laisserait croire qu'un second reproche a été enregistré.
        raise SignalementInvalide("Le même motif est coché deux fois.")

    return decidant(motifs).route


def est_ligne_rouge(motifs: list[str]) -> bool:
    """Vrai si l'un des motifs appartient à la ligne rouge.

    Passe par la FAMILLE et non par la route : `router` peut un jour rendre
    `RETRAIT_CONSERVATOIRE` pour une autre raison, et « est-ce une ligne rouge » doit
    continuer à répondre sur ce qui a été coché.
    """
    return any(motif(code).famille is Famille.ligne_rouge for code in motifs)


def valider_texte_libre(motifs: list[str], texte_libre: str | None) -> str | None:
    """Le texte libre normalisé, ou `None`. Lève si la règle n'est pas tenue.

    `autre` est le **seul** motif qui accepte un texte libre ; il y est **obligatoire**,
    et il est **refusé avec tous les autres**. Obligatoire parce qu'une case « autre »
    sans explication ne dit rien à qui doit trancher ; refusé ailleurs parce qu'un champ
    de commentaire attaché à « Incitation à la violence » deviendrait un canal de plus,
    non modéré, entre un signaleur et le responsable.
    """
    texte = (texte_libre or "").strip()
    if MOTIF_TEXTE_LIBRE in motifs:
        if not texte:
            raise SignalementInvalide(
                "Le motif « Autre raison » demande d'expliquer en quelques mots."
            )
        if len(texte) > TEXTE_LIBRE_MAX:
            raise SignalementInvalide(
                f"L'explication fait {len(texte)} caractères, "
                f"le maximum est {TEXTE_LIBRE_MAX}."
            )
        return texte
    if texte:
        raise SignalementInvalide(
            "Seul le motif « Autre raison » accepte une explication libre."
        )
    return None


# --- les trois gabarits de message à l'auteur -----------------------------------
#
# **LA RÈGLE, arrêtée par le client au MOD-3b et plus générale que le cas qui l'a fait
# écrire : aucun message n'annonce un effet que le code ne produit pas.**
#
# Elle a coûté la réécriture de deux gabarits sur trois. Le troisième disait « votre
# proposition est momentanément moins montrée » — or rien n'est déclassé nulle part : la
# diffusion réduite viendra du levier de tirage pondéré du C7, au MOD-12. Le premier
# disait « retirée temporairement » pour des routes (`A_REFORMULER`, `A_FUSIONNER`) qui
# ne retirent rien du tout. Les deux promettaient un site qui n'existe pas.
#
# `test_signalement.py` garde la règle : aucun message ne peut parler de retrait pour
# une route qui ne retire pas. Le jour où la diffusion réduite existera, on changera le
# texte AVEC le code, pas avant.
#
# Aucun de ces textes n'est encore envoyé — l'envoi de courriel reste hors périmètre.
# Ils sont affichés dans `/moderation/signalements` pour être relus.
#
# Chacun des trois porte, en fin de texte, le lien de contestation. Dès qu'un retrait
# devient visible par son auteur, l'exposé des motifs et la voie de recours sont dus.

#: Le préfixe de la page de contestation. **Elle existe depuis le MOD-3b** : elle attend
#: un jeton (`/contester/<jeton>`), non devinable, posé sur la proposition au moment du
#: retrait. Sans jeton, l'adresse ne mène nulle part — c'est voulu : il n'y a pas de
#: compte sur lequel s'appuyer, l'auteur d'une proposition étant très majoritairement
#: anonyme (164 participants sur 167 au 15 septembre 2026).
CHEMIN_CONTESTATION = "/contester"

#: La voie de recours, commune aux trois gabarits.
#:
#: Elle disait « contester cette décision » jusqu'au MOD-3b, et tombait sous la même
#: règle que les gabarits : sur deux routes de trois, **aucune décision n'a été prise** —
#: un examen est en cours, c'est tout. Nommer « décision » ce qui n'en est pas une
#: ferait croire à un verdict là où il n'y a qu'une plainte enregistrée.
RECOURS = "Si vous n'êtes pas d'accord, vous pouvez demander un réexamen : {lien}"

#: 1. Rattrapable — la proposition RESTE EN LIGNE, et l'auteur peut la reprendre.
#:
#: Ce gabarit disait « retirée temporairement » jusqu'au MOD-3b. C'était faux : les deux
#: routes qui l'emploient (`A_REFORMULER`, `A_FUSIONNER`) ne retirent rien — elles posent
#: une ligne dans la file du responsable. Ce qui le distingue du suivant n'est donc pas
#: un effet sur la proposition, c'est qu'il dit à l'auteur **ce qu'il peut faire**.
GABARIT_RATTRAPABLE = (
    "Un signalement concernant votre proposition est en cours d'examen : {motif}. "
    "Elle reste en ligne. Si vous souhaitez la reprendre, ce qui aide le plus est de "
    "s'en tenir à une seule idée, formulée dans vos mots, sans viser personne en "
    "particulier. Vous pouvez en proposer une nouvelle version à tout moment.\n\n"
    "{recours}"
)

#: 2. Non rattrapable — la proposition a réellement été retirée, et il n'y a rien à
#: modifier. Le seul des trois qui parle de retrait, et le seul dont la route en produise
#: un. Laisser croire à son auteur qu'une retouche la ramènerait serait l'inviter à
#: recommencer.
GABARIT_NON_RATTRAPABLE = "Votre proposition a été retirée car {motif}.\n\n{recours}"

#: 3. Examen en cours — rien n'est demandé à l'auteur, rien ne change pour sa proposition.
#:
#: Il s'appelait `GABARIT_MESURE_CONSERVATOIRE` jusqu'au MOD-3b, et le nom était aussi
#: trompeur que le texte : aucune mesure n'est prise. Le renommer plutôt que de garder
#: l'ancien nom sur un texte neuf évite qu'on le rebranche un jour sur la foi de son
#: intitulé — « mesure conservatoire » reste réservé au retrait, qui en est une.
GABARIT_EXAMEN_EN_COURS = (
    "Un signalement concernant votre proposition est en cours d'examen. Elle reste en "
    "ligne.\n\n{recours}"
)

#: Quel gabarit pour quelle route. Le choix est à nous, la consigne ne l'arrête pas :
#:
#:  - `RETRAIT_CONSERVATOIRE` -> **non rattrapable** : c'est la seule route qui retire,
#:    et un contenu de ligne rouge ne se reformule pas ;
#:  - `A_REFORMULER` et `A_FUSIONNER` -> **rattrapable** : rien n'est retiré, mais
#:    l'auteur peut agir, et c'est la seule famille de cas où lui dire comment a un sens ;
#:  - `A_QUALIFIER` et `FILE_RESPONSABLE` -> **examen en cours** : rien ne lui est
#:    demandé, rien ne change, on lui dit seulement qu'un examen a lieu.
GABARIT_PAR_ROUTE: dict[Route, str] = {
    Route.RETRAIT_CONSERVATOIRE: GABARIT_NON_RATTRAPABLE,
    Route.A_QUALIFIER: GABARIT_EXAMEN_EN_COURS,
    Route.A_REFORMULER: GABARIT_RATTRAPABLE,
    Route.A_FUSIONNER: GABARIT_RATTRAPABLE,
    Route.FILE_RESPONSABLE: GABARIT_EXAMEN_EN_COURS,
}

#: Les routes qui retirent réellement quelque chose. Sert à `message_a_l_auteur` et au
#: test qui tient la règle : une seule liste, pour qu'un ajout de route ne puisse pas
#: faire diverger le message de l'effet.
ROUTES_QUI_RETIRENT: frozenset[Route] = frozenset({Route.RETRAIT_CONSERVATOIRE})

#: Les routes qui laissent l'auteur agir — **dérivées de la table ci-dessus, jamais
#: recopiées.** Une liste écrite à la main aurait vieilli au premier ajout de route :
#: c'est le gabarit qui dit si l'auteur a quelque chose à faire, pas une seconde liste
#: qu'il faudrait penser à tenir à jour.
#:
#: Le MOD-15 s'en sert pour décider quels signalements valent un rappel à l'auteur : on
#: n'avertit que là où il peut faire quelque chose. Prévenir sans rien proposer serait
#: inquiéter pour rien.
ROUTES_RATTRAPABLES: frozenset[Route] = frozenset(
    route for route, gabarit in GABARIT_PAR_ROUTE.items() if gabarit is GABARIT_RATTRAPABLE
)


def lien_de_contestation(jeton: str | None = None) -> str:
    """L'adresse de la voie de recours, telle qu'elle partirait dans un message.

    Sans jeton, l'adresse générique — c'est ce que montre l'écran du responsable quand
    il relit un message pour une proposition qui n'a pas été retirée, donc qui n'a pas
    de jeton. Avec, l'adresse réellement ouvrable par l'auteur.
    """
    base = f"{settings.public_base_url.rstrip('/')}{CHEMIN_CONTESTATION}"
    return f"{base}/{jeton}" if jeton else base


def message_a_l_auteur(
    route: Route,
    motifs: list[str],
    jeton: str | None = None,
    avec_recours: bool = True,
) -> str:
    """Le message qui serait adressé à l'auteur, lien de recours compris.

    « Serait » : aucun envoi dans ce lot. L'écran du responsable l'affiche pour qu'il
    soit relu — c'est la seule façon de s'apercevoir avant l'envoi qu'un gabarit dit
    quelque chose que le site ne fait pas. C'est exactement ce qui est arrivé au MOD-3a,
    et ce qui a produit la règle en tête de cette section.

    Le motif cité est celui qui a **décidé** de la route, pas le premier saisi : c'est
    lui qu'il faut exposer, sans quoi un message dirait « retirée car doublon » pour une
    proposition retirée pour incitation à la violence.

    `avec_recours=False` retire la voie de recours, et n'existe que pour le cas où elle
    **ne mène nulle part** : le lien de contestation exige un jeton, et le jeton n'est
    posé qu'au retrait. Montrer à l'auteur d'une proposition encore en ligne un
    « demander un réexamen » qui tombe sur une page sans jeton serait lui promettre une
    porte qui n'ouvre pas. Le défaut reste `True` : l'écran du responsable, lui, affiche
    délibérément l'adresse générique quand il relit un message.
    """
    recours = RECOURS.format(lien=lien_de_contestation(jeton)) if avec_recours else ""
    return (
        GABARIT_PAR_ROUTE[route]
        .format(motif=decidant(motifs).phrase, recours=recours)
        .rstrip()
    )


# --- les données de contexte, et la précaution qui va avec -----------------------
#
# Pour pouvoir détecter plus tard un afflux coordonné (MOD-5), il faut capter au moment
# du signalement ce qui ne se reconstruit pas après coup : le référent externe et
# l'adresse IP. Ce sont des données personnelles — d'où, sans exception : haché, tronqué,
# jamais en clair, et purgé à 30 jours (`purge-contexte-signalements`).

#: Longueur du condensé conservé, en caractères hexadécimaux. 16 hex = 64 bits : assez
#: pour que deux signalements venus de la même origine se reconnaissent entre eux sans
#: collision à l'échelle de ce site, trop peu pour constituer un identifiant durable.
#: Ce qui protège vraiment n'est pas cette troncature mais le **sel** : sans
#: `settings.secret_key`, une adresse IPv4 se retrouve par force brute en quelques
#: secondes, 32 bits d'espace d'adressage étant ce qu'ils sont.
CONDENSE_LONGUEUR = 16


def condenser(valeur: str | None) -> str | None:
    """Un condensé salé et tronqué, ou `None`. **Jamais réversible, jamais en clair.**

    Salé par `settings.secret_key`, qui ne sort pas du serveur : le condensé ne sert
    qu'à comparer deux signalements entre eux, jamais à remonter à quelqu'un. La clé
    est tronquée à 32 octets, maximum admis par blake2s.
    """
    if not valeur:
        return None
    empreinte = hashlib.blake2s(
        valeur.encode("utf-8"),
        key=settings.secret_key.encode("utf-8")[:32],
        digest_size=CONDENSE_LONGUEUR // 2,
    )
    return empreinte.hexdigest()


def origine_externe(referent: str | None) -> str | None:
    """L'origine d'un référent, seulement s'il est EXTERNE au site. `None` sinon.

    Ce qu'on cherche à reconnaître est « beaucoup de signalements arrivés depuis le même
    ailleurs ». Un référent interne — la page d'un débat, l'accueil — ne dit rien de tel
    et serait le cas de 99 % des lignes : le garder noierait le signal dans le bruit, et
    conserverait une donnée de navigation sans usage.

    L'origine seule (`https://exemple.fr`), jamais le chemin : le chemin d'un lien
    partagé peut porter un identifiant de campagne, un pseudonyme, une recherche.
    """
    if not referent:
        return None
    morceaux = urlsplit(referent)
    if not morceaux.scheme or not morceaux.netloc:
        return None
    interne = urlsplit(settings.public_base_url).netloc
    if morceaux.netloc == interne:
        return None
    return f"{morceaux.scheme}://{morceaux.netloc}"


#: La phrase à coller dans la politique de confidentialité **avant toute mise en
#: production** du signalement. Elle est ici, dans le code, et non seulement dans le
#: journal : c'est le fichier que lira celui qui touchera à `condenser`.
NOTE_CONFIDENTIALITE = (
    "Lorsque vous signalez une proposition, nous conservons pendant trente jours au "
    "maximum une empreinte de votre adresse IP et du site depuis lequel vous êtes "
    "arrivé. Ces empreintes sont calculées avec une clé secrète détenue par le serveur : "
    "elles permettent de reconnaître que plusieurs signalements proviennent d'une même "
    "origine, jamais de remonter à votre identité. Cette conservation a pour seule "
    "finalité la prévention des abus — signalements coordonnés visant à faire retirer "
    "une proposition légitime. Passé trente jours, ces empreintes sont effacées ; le "
    "signalement, lui, est conservé."
)
