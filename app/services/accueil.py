"""Ce que la page d'accueil affiche en plus de la liste : tri et chiffres d'ambiance.

Réuni ici plutôt que dispersé dans la vue, parce que ce sont des décisions et non des
requêtes : ce qui compte comme « proposition ouverte », ce qui compte comme
« participant de la semaine », et ce que veut dire trier par nombre de votes. Chacune
se teste toute seule.

**Aucun de ces chiffres n'est un total du site.** Ce sont trois questions différentes,
et les confondre donnerait des nombres qui ne s'additionnent pas :
  - les propositions ouvertes ne comptent que les conversations **ouvertes au vote** ;
  - le tri par votes compte les votes de **toutes** les conversations publiées, closes
    comprises — une consultation close reste triable par ce qu'elle a réuni ;
  - les participants de la semaine comptent des **personnes**, pas des votes.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.matching import group_name
from app.services import recalcul
from app.services.carte import Enveloppe
from app.services.carte_rendu import couleurs_par_groupe
from app.models import (
    AnalysisRun,
    AnalysisStatus,
    Conversation,
    ConversationState,
    ConversationTheme,
    ModerationStatus,
    ParticipantProjection,
    Statement,
    StatementStat,
    Vote,
)
from app.services import clivage, resultats
from app.services.groups import MIN_GROUP_SIZE
from app.services.themes import themes_connus

#: Les quatre tris connus. Les deux derniers sont arrivés avec le chantier L, une fois
#: le site capable de mesurer ce que les mots veulent dire — ils étaient écartés depuis
#: le 4 septembre 2026 faute de définition, et non faute de place dans la barre.
#:
#: **« Le plus consensuel » n'est pas l'inverse de « Le plus clivant ».** Les deux
#: colonnes qu'ils lisent sont indépendantes : `clivage` moyenne le haut du classement
#: des propositions d'un débat, `consensus` en moyenne le bas (voir `services/clivage`).
#: Un débat peut donc être bien placé dans les deux listes — cinq points de fracture
#: nets et cinq accords qui tiennent malgré eux —, et c'est même le débat que le site a
#: le plus de raisons de montrer. Deux onglets, et non un onglet avec deux sens.
TRIS = {
    "recent": "Récent",
    "votes": "Le plus voté",
    "clivant": "Le plus clivant",
    "consensuel": "Le plus consensuel",
}

#: Le tri par défaut n'est PAS le clivage, et ce n'est pas un réglage. Ce qu'un site
#: ordonne par défaut est une position éditoriale : mettre les débats les plus divisés
#: en tête d'un site dont la mission affichée est de dégager des accords dirait le
#: contraire de ce que la page d'accueil promet. Même raisonnement qu'au J0 sur
#: « filtrer ou mettre en avant ».
TRI_DEFAUT = "recent"

#: Les tris qui ORDONNENT la liste des débats sur une mesure, et laissent donc les
#: débats non mesurés à la fin — c'est ce qui oblige l'écran à le dire.
#:
#: « Le plus consensuel » n'y figure plus depuis le L6 : il ne classe plus des débats,
#: il liste des propositions, et il n'a donc pas de queue de liste à expliquer.
TRIS_ORDONNANT_SUR_MESURE = ("clivant",)

#: Les tris qui reposent sur une MESURE, et qui ne s'affichent donc que si elle existe.
#: Un onglet qui rendrait exactement la même liste que « Récent » — l'état du site tant
#: qu'aucun débat n'est mesuré — n'apprend rien et fait douter des autres. Même
#: raisonnement qu'au J6 pour le troisième onglet, absent tant que le visiteur n'a pas
#: réglé ses thèmes.
#:
#: Les deux apparaissent et disparaissent ENSEMBLE : `clivage.agrege` écrit les deux
#: scores d'un même geste, ou aucun. Un site où l'un des deux onglets manquerait
#: n'existe pas.
TRIS_CONDITIONNELS = ("clivant", "consensuel")

#: Fenêtre de « En ce moment ». Sept jours et non trente : sur un site qui recalcule
#: ses groupes toutes les heures, un chiffre mensuel ne dirait rien de l'activité du
#: moment — il ne bougerait quasiment jamais.
FENETRE_JOURS = 7


def tri_valide(demande: str | None, proposes: dict[str, str] | None = None) -> str:
    """Ramène n'importe quelle valeur d'URL à un tri **proposé sur cet écran**.

    Un paramètre inconnu ou absent donne le tri par défaut plutôt qu'une erreur : une
    URL partagée avec une faute de frappe doit afficher la page, pas un 422.

    `proposes` est ce que `tris_proposes` a retenu pour la requête en cours. Le passer
    fait retomber `?tri=clivant` sur « Récent » tant qu'aucun débat n'est mesuré : sans
    lui, une adresse recopiée demanderait un classement dont l'onglet n'existe pas, et
    la barre n'aurait aucun onglet marqué comme actif.
    """
    proposes = TRIS if proposes is None else proposes
    return demande if demande in proposes else TRI_DEFAUT


def tri_sur_mesure(tri: str) -> bool:
    """Ce tri laisse-t-il des débats non mesurés à la fin de la liste ?

    Lue par la barre de liste, qui doit alors dire pourquoi la fin de la liste n'est
    pas ce qu'elle semble être. C'est une décision, pas une requête : elle vit ici et
    non dans le gabarit, sans quoi la liste des tris concernés serait écrite à deux
    endroits et finirait par différer.
    """
    return tri in TRIS_ORDONNANT_SUR_MESURE


def liste_de_propositions(tri: str) -> bool:
    """Cet onglet montre-t-il des propositions plutôt que des débats ?

    **C'est la particularité du L6, et elle mérite d'être nommée.** Trois onglets
    ORDONNENT la même liste de débats ; le quatrième en change l'objet. Un écran qui
    changerait de contenu sans que rien ne le dise passerait pour une panne — d'où le
    chapeau que la macro affiche, et d'où cette fonction : le gabarit demande au
    service ce qu'il doit rendre, il ne le déduit pas d'une comparaison de chaîne.
    """
    return tri == "consensuel"


@dataclass(frozen=True)
class Filtre:
    """Ce qui RESTREINT la liste, par opposition au tri, qui l'ORDONNE.

    Les deux sont orthogonaux et se conservent l'un l'autre : c'est la règle posée au
    J0 et rappelée par le client — « un clic sur *Le plus voté* ne doit pas effacer
    silencieusement le filtre ».

    Deux origines, exclusives entre elles :

      - `theme` — un seul code, venu d'une étiquette cliquée ;
      - `mes_themes` — l'union des thèmes du visiteur, venue du troisième onglet.

    **Elles ne se cumulent pas.** Si les deux arrivent dans une même adresse — ce que
    nos liens n'émettent jamais, mais qu'une URL recopiée peut contenir —, `theme`
    l'emporte : c'est le geste le plus explicite et le plus récent des deux.
    """

    theme: str | None = None
    mes_themes: tuple[str, ...] = ()

    @property
    def actif(self) -> bool:
        return bool(self.codes)

    @property
    def codes(self) -> tuple[str, ...]:
        """Les codes qui restreignent réellement la liste."""
        if self.theme:
            return (self.theme,)
        return self.mes_themes

    @property
    def sur_mes_themes(self) -> bool:
        """Vrai quand c'est l'onglet qui filtre, et non une étiquette."""
        return not self.theme and bool(self.mes_themes)


def filtre_valide(
    theme: str | None, mes_themes: bool, themes_du_visiteur: list[str] | None
) -> Filtre:
    """Ramène les paramètres d'URL à un filtre applicable.

    Même règle que `tri_valide` : **rien ne lève**. Un code inconnu, un visiteur sans
    préférence, un `mes-themes=1` recopié à la main — tout cela affiche la liste
    entière plutôt qu'une erreur. Une adresse partagée avec une faute de frappe doit
    montrer la page.
    """
    codes = themes_connus([theme] if theme else [])
    if codes:
        return Filtre(theme=codes[0])
    if mes_themes:
        return Filtre(mes_themes=tuple(themes_connus(themes_du_visiteur)))
    return Filtre()


def appliquer_filtre(requete: Select, filtre: Filtre) -> Select:
    """Restreint la liste aux débats portant l'un des codes.

    `IN (sous-requête)` et non une jointure : un débat portant DEUX des codes demandés
    sortirait deux fois d'une jointure, et il faudrait un `DISTINCT` pour le rattraper —
    lequel se combine mal avec l'ordre par nombre de votes du tri. La sous-requête ne
    duplique rien et se compose avec n'importe quel tri.
    """
    if not filtre.actif:
        return requete
    return requete.where(
        Conversation.id.in_(
            select(ConversationTheme.conversation_id).where(
                ConversationTheme.code.in_(filtre.codes)
            )
        )
    )


def adresse(base: str, *, tri: str, filtre: Filtre | None = None, **surcharges) -> str:
    """Construit une adresse de liste en CONSERVANT ce qui n'est pas surchargé.

    Écrite ici et non dans les gabarits parce que trois écrans la construisent — accueil,
    page « Débats », fournées — et qu'une adresse qui perd le filtre sur un seul d'entre
    eux est un défaut qu'on ne voit qu'en cliquant.

    Les surcharges acceptent `None` pour RETIRER un paramètre : c'est ainsi que se
    fabrique le lien « voir tous les débats » qui sort du filtre.
    """
    filtre = filtre or Filtre()
    params: dict[str, str] = {"tri": tri}
    if filtre.theme:
        params["theme"] = filtre.theme
    elif filtre.mes_themes:
        params["mes-themes"] = "1"
    for cle, valeur in surcharges.items():
        cle = cle.replace("_", "-")
        if valeur is None:
            params.pop(cle, None)
            # Poser `theme` retire `mes-themes`, et l'inverse : les deux filtres sont
            # exclusifs, et les laisser cohabiter dans une adresse donnerait un lien
            # dont l'effet dépend d'une règle de priorité que personne ne lit.
        else:
            if cle == "theme":
                params.pop("mes-themes", None)
            if cle == "mes-themes":
                params.pop("theme", None)
            params[cle] = str(valeur)
    return f"{base}?{urlencode(params)}"


def _votes_par_conversation():
    """Sous-requête : conversation_id -> nombre de votes, propositions approuvées seules.

    Une proposition rejetée après coup ne doit plus peser sur le classement, au même
    titre qu'elle ne pèse plus sur l'analyse.
    """
    return (
        select(
            Statement.conversation_id.label("conversation_id"),
            func.count(Vote.id).label("n_votes"),
        )
        .join(Vote, Vote.statement_id == Statement.id)
        .where(Statement.moderation_status == ModerationStatus.approved)
        .group_by(Statement.conversation_id)
        .subquery()
    )


def _dernier_calcul_par_conversation():
    """Sous-requête : le DERNIER calcul abouti de chaque conversation, et ses scores.

    Sans restriction d'identifiants, contrairement à `_derniers_calculs_aboutis` : le
    tri s'applique à la liste entière avant qu'on sache quels débats elle contient.

    L'identifiant départage les calculs à `finished_at` identique. Ce n'est pas une
    précaution de style : deux calculs de la même seconde — ce qui arrive au rattrapage
    d'une file en retard — donneraient sinon un « dernier calcul » différent d'une
    requête à l'autre, donc un ordre de liste instable, donc une pagination qui saute
    des débats.
    """
    classement = (
        select(
            AnalysisRun.id.label("run_id"),
            AnalysisRun.conversation_id.label("conversation_id"),
            AnalysisRun.clivage.label("clivage"),
            AnalysisRun.consensus.label("consensus"),
            func.row_number()
            .over(
                partition_by=AnalysisRun.conversation_id,
                order_by=(AnalysisRun.finished_at.desc(), AnalysisRun.id.desc()),
            )
            .label("rang"),
        )
        .where(AnalysisRun.status == AnalysisStatus.ok)
        .subquery()
    )
    return (
        select(
            classement.c.run_id,
            classement.c.conversation_id,
            classement.c.clivage,
            classement.c.consensus,
        )
        .where(classement.c.rang == 1)
        .subquery()
    )


async def scores_mesures(session: AsyncSession) -> bool:
    """Au moins un débat visible a-t-il été mesuré ?

    C'est la condition d'apparition des DEUX onglets de mesure. Une seule question et
    non deux : `clivage.agrege` écrit `clivage` et `consensus` d'un même geste, ou
    n'écrit ni l'un ni l'autre. Interroger la seconde colonne séparément donnerait
    toujours la même réponse, pour une requête de plus.

    Une seule requête, et elle s'arrête au premier débat trouvé.
    """
    dernier = _dernier_calcul_par_conversation()
    requete = (
        liste_publiee()
        .join(dernier, dernier.c.conversation_id == Conversation.id)
        .where(dernier.c.clivage.isnot(None))
        .limit(1)
    )
    return (await session.scalar(select(requete.exists()))) is True


async def tris_proposes(session: AsyncSession) -> dict[str, str]:
    """Les onglets de tri à montrer sur cet écran, à cet instant.

    `TRIS` dit ce que le site sait faire ; cette fonction dit ce qu'il a de quoi faire.
    Les deux diffèrent tant qu'aucun débat n'est mesuré, et c'est le seul cas.
    """
    if await scores_mesures(session):
        return dict(TRIS)
    return {
        cle: intitule
        for cle, intitule in TRIS.items()
        if cle not in TRIS_CONDITIONNELS
    }


def appliquer_tri(requete: Select, tri: str) -> Select:
    """Ajoute l'ordre demandé à la requête de la liste des débats.

    **L'identifiant clôt toujours l'ordre**, et ce n'est pas une précaution de style
    depuis le G10 : la liste est désormais paginée, et une page se demande par un
    décalage. Deux débats que l'ordre ne départage pas — même seconde de création, ou
    même nombre de votes, ce qui est fréquent à zéro vote — peuvent alors sortir dans
    un ordre différent d'une requête à l'autre, et « charger la suite » sauterait un
    débat ou en montrerait deux fois le même. L'identifiant est unique et immuable :
    il rend l'ordre total, donc la pagination sûre.
    """
    if tri == "clivant":
        dernier = _dernier_calcul_par_conversation()
        return (
            requete.outerjoin(
                dernier, dernier.c.conversation_id == Conversation.id
            )
            # `nullslast` et non `coalesce(…, 0)` : un débat sans calcul abouti n'a pas
            # un clivage nul, il n'a PAS de clivage — c'est la règle du L1, et la
            # remplacer par un zéro le déclarerait consensuel. PostgreSQL place les
            # NULL en tête d'un ordre décroissant : sans cette mention, la liste « la
            # plus clivante » commencerait par les débats que personne n'a mesurés.
            .order_by(
                dernier.c.clivage.desc().nullslast(),
                Conversation.created_at.desc(),
                Conversation.id.desc(),
            )
        )
    if tri == "votes":
        compte = _votes_par_conversation()
        return (
            requete.outerjoin(
                compte, compte.c.conversation_id == Conversation.id
            )
            # `coalesce` : une conversation sans aucun vote n'a pas de ligne dans la
            # sous-requête. Sans lui, elle sortirait avec un NULL, que PostgreSQL
            # place AVANT tout le reste en ordre décroissant — la liste « la plus
            # votée » commencerait donc par les débats que personne n'a votés.
            .order_by(
                func.coalesce(compte.c.n_votes, 0).desc(),
                Conversation.created_at.desc(),
                Conversation.id.desc(),
            )
        )
    return requete.order_by(Conversation.created_at.desc(), Conversation.id.desc())


#: Débats montrés sur l'accueil, par onglet. Cinq : l'accueil est une vitrine, pas un
#: catalogue — il montre de quoi donner envie, et « Voir tous les débats » mène à la
#: liste. Décision du client du 5 septembre 2026.
LIMITE_ACCUEIL = 5

#: Débats par fournée sur la page « Débats ». Dix à l'arrivée, dix de plus à chaque
#: fois qu'on descend.
LIMITE_DEBATS = 10

#: Plafond de ce qu'une seule requête peut faire rendre. Le nombre de débats affichés
#: vient de l'URL — c'est ce qui rend le défilement partageable et utilisable sans
#: JavaScript —, et une valeur venue de l'URL se borne. Sans ce plafond, `?nombre=99999`
#: ferait rendre le catalogue entier, avec sa mini-barre et sa répartition, sur une
#: simple requête anonyme. Vingt fournées : bien au-delà de ce qu'on déroule à la main.
PLAFOND_DEBATS = 200


def nombre_valide(demande: int, mini: int = 1) -> int:
    """Ramène un nombre venu de l'URL dans les bornes admises.

    Comme `tri_valide` : une valeur aberrante affiche la page, elle ne lève pas. Une
    adresse recopiée à la main n'est pas une attaque, et un 422 en réponse à une faute
    de frappe ferait perdre la liste à quelqu'un qui voulait juste la lire.
    """
    try:
        valeur = int(demande)
    except (TypeError, ValueError):
        return LIMITE_DEBATS
    return max(mini, min(valeur, PLAFOND_DEBATS))


def liste_publiee() -> Select:
    """Les débats visibles de tous : publics, publiés, approuvés.

    Une seule définition, employée par l'accueil, par la page « Débats » et par le
    chargement de la suite. Écrite trois fois, elle finirait par différer d'un écran à
    l'autre — et un débat visible ici serait introuvable là.
    """
    return select(Conversation).where(
        Conversation.is_public.is_(True),
        Conversation.state != ConversationState.draft,
        Conversation.moderation_status == ModerationStatus.approved,
    )


async def page_de_debats(
    session: AsyncSession,
    tri: str,
    limite: int,
    decalage: int = 0,
    filtre: Filtre | None = None,
) -> tuple[list[Conversation], bool]:
    """Une tranche de la liste, et s'il en reste après elle.

    Le « il en reste » est obtenu en demandant **un débat de plus** que la tranche, puis
    en le jetant. Un `COUNT(*)` séparé aurait coûté une seconde requête sur toute la
    table à chaque défilement, pour n'apprendre qu'un booléen — et il aurait pu
    contredire la tranche, comptée à un autre instant.
    """
    requete = (
        appliquer_tri(appliquer_filtre(liste_publiee(), filtre or Filtre()), tri)
        .offset(decalage)
        .limit(limite + 1)
    )
    trouves = list(await session.scalars(requete))
    return trouves[:limite], len(trouves) > limite


async def propositions_ouvertes(
    session: AsyncSession, filtre: Filtre | None = None
) -> int:
    """Propositions sur lesquelles on peut voter en ce moment.

    « Ouvertes » se prend au pied de la lettre : approuvées, dans une conversation
    publique, publiée, approuvée, et dont l'état est `open`. Une proposition d'une
    consultation close n'est plus ouverte au vote, et l'annoncer comme telle serait
    une invitation à cliquer sur ce qui n'attend plus personne.

    **Le compteur suit le filtre.** Annoncer « 128 propositions ouvertes » au-dessus
    d'une liste de trois débats filtrés est une contradiction visible à l'œil nu, et
    c'est le genre de détail qui fait douter de tout le reste de la page.
    """
    requete = (
        select(func.count(Statement.id))
        .join(Conversation, Conversation.id == Statement.conversation_id)
        .where(
            Statement.moderation_status == ModerationStatus.approved,
            Conversation.is_public.is_(True),
            Conversation.state == ConversationState.open,
            Conversation.moderation_status == ModerationStatus.approved,
        )
    )
    return await session.scalar(appliquer_filtre(requete, filtre or Filtre())) or 0


async def participants_de_la_semaine(session: AsyncSession) -> int:
    """Personnes DISTINCTES ayant voté au moins une fois sur les sept derniers jours.

    Des personnes, pas des votes : quelqu'un qui répond à trente propositions en une
    session compte pour un. C'est ce que « 184 participants cette semaine » promet, et
    compter les votes gonflerait le chiffre d'un ordre de grandeur.

    `created_at` et non `modified_at` : un re-vote sur une vieille proposition change
    un avis, il ne fait pas revenir quelqu'un.
    """
    depuis = datetime.now(timezone.utc) - timedelta(days=FENETRE_JOURS)
    return await session.scalar(
        select(func.count(func.distinct(Vote.participant_id))).where(
            Vote.created_at >= depuis
        )
    ) or 0


async def total_votes(session: AsyncSession) -> int:
    """Votes exprimés sur des propositions approuvées de débats visibles (chantier I).

    Sert au bloc « le site en chiffres » de l'accueil, à côté de
    `participants_de_la_semaine` et `propositions_ouvertes` — mais c'est une TROISIÈME
    question : tous les votes, de toutes les conversations visibles (closes comprises,
    comme le tri par votes), sans fenêtre de temps. Ne pas la confondre avec les deux
    autres, ni tenter de les faire s'additionner.
    """
    requete = (
        select(func.count(Vote.id))
        .join(Statement, Statement.id == Vote.statement_id)
        .join(Conversation, Conversation.id == Statement.conversation_id)
        .where(
            Statement.moderation_status == ModerationStatus.approved,
            Conversation.is_public.is_(True),
            Conversation.state != ConversationState.draft,
            Conversation.moderation_status == ModerationStatus.approved,
        )
    )
    return await session.scalar(requete) or 0


#: Réexportée : la fonction a déménagé dans `app/services/recalcul.py` au G9, quand le
#: message de fin de parcours a eu besoin de la MÊME cadence pour dire le temps restant.
#: Le nom reste appelable ici — deux gabarits et deux routes l'emploient sous ce nom, et
#: une cadence annoncée depuis deux modules finirait par être annoncée en deux valeurs.
periode_de_recalcul = recalcul.periode_de_recalcul


# --- Répartition des groupes, par débat (chantier G3) -----------------------------

#: Ancienneté en dessous de laquelle un débat est annoncé « Nouveau ». Sept jours,
#: la même fenêtre que « participants cette semaine » : deux durées différentes sur
#: le même écran auraient demandé au lecteur de retenir laquelle s'applique où.
NOUVEAU_JOURS = 7

@dataclass
class Segment:
    """Un groupe dans la mini-barre."""

    nom: str
    effectif: int
    #: Part en pourcentage, arrondie. Sert de largeur CSS.
    part: float
    #: Couleur du groupe, EXACTEMENT celle qu'il porte sur la carte de sa page —
    #: `carte_rendu.couleurs_par_groupe` est appelée ici, elle n'est pas réimplémentée.
    #: Elle suit l'IDENTITÉ du groupe à travers son histoire, et non son effectif ni son
    #: rang : deux groupes qui échangent leur taille n'échangent pas leur teinte.
    couleur: str


@dataclass
class Repartition:
    """Ce qu'une ligne de la liste affiche à côté de son titre."""

    n_votes: int = 0
    segments: list[Segment] = field(default_factory=list)
    #: Faux tant qu'aucun calcul n'a abouti : la barre est alors grise, et le dire
    #: vaut mieux que de la montrer vide, ce qui se lirait comme « personne ».
    calculee: bool = False
    #: Combien de propositions du dernier calcul dépassent `clivage.SEUIL_CLIVANTE`.
    #: `None` tant que rien n'est mesuré, et ce n'est pas zéro : « pas encore mesuré »
    #: n'est pas « aucune ». La carte se tait dans les deux cas, mais la distinction est
    #: vraie en base et le reste pour qui relira ce champ.
    n_clivantes: int | None = None
    #: L'étiquette « Clivant » est-elle méritée ? Deux calculs consécutifs au moins,
    #: voir `clivage.porte_etiquette`.
    clivant: bool = False

    @property
    def n_groupes(self) -> int:
        return len(self.segments)


def est_nouvelle(conversation: Conversation, maintenant: datetime | None = None) -> bool:
    """Un débat ouvert depuis moins d'une semaine."""
    maintenant = maintenant or datetime.now(timezone.utc)
    return (maintenant - conversation.created_at) < timedelta(days=NOUVEAU_JOURS)


def depuis_ouverture(
    conversation: Conversation, maintenant: datetime | None = None
) -> str | None:
    """Depuis quand ce débat est ouvert, en toutes lettres — ou `None` s'il ne l'est
    plus depuis assez peu de temps pour que ça vaille la peine de le dire.

    Remplace l'étiquette « Nouveau » sur les cartes (chantier I2, instruction 8) : le
    mot ne disait pas *à quel point*, et deux débats ouverts à six jours d'écart le
    portaient à l'identique.

    La FENÊTRE ne change pas : c'est celle d'`est_nouvelle`, sept jours, et cette
    fonction s'appuie dessus plutôt que de la recopier — deux règles pour la même
    décision finiraient par diverger. Un débat de huit jours n'a donc pas d'étiquette du
    tout, comme avant.

    Les seuils du texte, en jours entiers écoulés :

        0 jour   « Ouvert aujourd'hui »
        1 jour   « Ouvert hier »
        2 à 6    « Ouvert il y a N jours »

    « Il y a 0 jour » et « il y a 1 jour » sont des tournures que personne n'emploie ;
    au-delà, le compte en jours est ce qui se lit le plus vite. Le pluriel commence à
    deux, donc « jours » est toujours au pluriel dans cette branche.
    """
    maintenant = maintenant or datetime.now(timezone.utc)
    if not est_nouvelle(conversation, maintenant):
        return None
    jours = (maintenant - conversation.created_at).days
    if jours <= 0:
        return "Ouvert aujourd'hui"
    if jours == 1:
        return "Ouvert hier"
    return f"Ouvert il y a {jours} jours"


def _derniers_calculs_aboutis(ids: list[int]):
    """Sous-requête : l'id du DERNIER calcul abouti de chaque conversation.

    Une fonction de fenêtrage plutôt qu'une requête par conversation : la liste
    d'accueil en compterait autant que de débats affichés, et c'est exactement le
    genre de N+1 qui ne se voit pas tant que la liste est courte.
    """
    classement = (
        select(
            AnalysisRun.id.label("run_id"),
            AnalysisRun.conversation_id.label("conversation_id"),
            func.row_number()
            .over(
                partition_by=AnalysisRun.conversation_id,
                order_by=AnalysisRun.finished_at.desc(),
            )
            .label("rang"),
        )
        .where(
            AnalysisRun.status == AnalysisStatus.ok,
            AnalysisRun.conversation_id.in_(ids),
        )
        .subquery()
    )
    return (
        select(classement.c.run_id, classement.c.conversation_id)
        .where(classement.c.rang == 1)
        .subquery()
    )


async def repartitions(
    session: AsyncSession, conversations: list[Conversation]
) -> dict[int, Repartition]:
    """Votes et répartition des groupes, pour toutes les conversations affichées.

    **Trois** requêtes au total, quel que soit le nombre de débats — pas trois par
    débat. La troisième est celle des couleurs, et elle ne lit qu'une colonne JSON
    minuscule par calcul (`analysis_run.group_mapping`), jamais la table des
    projections.

    Ne compte que les participants **classés** (`stable_group_id` non nul), comme la
    carte : quelqu'un qui n'a pas assez voté pour être situé n'appartient à aucun
    groupe, et le compter quelque part reviendrait à en inventer un.
    """
    if not conversations:
        return {}

    ids = [c.id for c in conversations]
    resultat = {identifiant: Repartition() for identifiant in ids}

    # --- 1. les votes
    compte = (
        select(
            Statement.conversation_id.label("conversation_id"),
            func.count(Vote.id).label("n_votes"),
        )
        .join(Vote, Vote.statement_id == Statement.id)
        .where(
            Statement.moderation_status == ModerationStatus.approved,
            Statement.conversation_id.in_(ids),
        )
        .group_by(Statement.conversation_id)
    )
    for conversation_id, n_votes in (await session.execute(compte)).all():
        resultat[conversation_id].n_votes = n_votes or 0

    # --- 2. la répartition du dernier calcul abouti
    derniers = _derniers_calculs_aboutis(ids)
    effectifs = (
        select(
            derniers.c.conversation_id,
            ParticipantProjection.stable_group_id,
            func.count(ParticipantProjection.id).label("effectif"),
        )
        .join(derniers, derniers.c.run_id == ParticipantProjection.run_id)
        .where(ParticipantProjection.stable_group_id.isnot(None))
        .group_by(derniers.c.conversation_id, ParticipantProjection.stable_group_id)
    )

    brut: dict[int, list[tuple[int, int]]] = {}
    for conversation_id, stable, effectif in (await session.execute(effectifs)).all():
        brut.setdefault(conversation_id, []).append((stable, effectif))

    # --- 3. les couleurs, lues de la MÊME source que la carte
    for conversation_id, lignes in brut.items():
        total = sum(effectif for _, effectif in lignes)
        if not total:
            continue
        # Trié par effectif décroissant depuis le G10, le nom départageant les égalités.
        # C'est la conséquence directe de « le plus grand groupe est bleu » : avec des
        # teintes rangées par taille et des segments rangés par identité, la barre
        # afficherait ses couleurs dans le désordre de la palette, et le plus large
        # segment pourrait se trouver au milieu.
        lignes.sort(key=lambda ligne: (-ligne[1], group_name(ligne[0]) or "?"))
        # `couleurs_par_groupe` est appelée telle quelle, et non réécrite : c'est elle
        # qui décide de la teinte d'un groupe sur la carte de sa page. Une seconde
        # implémentation, même fidèle le jour où on l'écrit, finirait par diverger — et
        # le groupe A serait bleu ici, magenta une page plus loin.
        couleurs = couleurs_par_groupe(
            [
                Enveloppe(name=group_name(stable) or "?", size=effectif, stable_id=stable)
                for stable, effectif in lignes
            ]
        )
        resultat[conversation_id] = Repartition(
            n_votes=resultat[conversation_id].n_votes,
            calculee=True,
            segments=[
                Segment(
                    nom=group_name(stable) or "?",
                    effectif=effectif,
                    part=round(effectif / total * 100, 2),
                    couleur=couleurs[group_name(stable) or "?"],
                )
                for stable, effectif in lignes
            ],
        )

    # --- 4. le clivage : le compte du dernier calcul, l'étiquette sur les deux derniers
    #
    # Posé APRÈS l'étape 3, qui RECONSTRUIT l'objet : l'écrire avant le ferait
    # silencieusement disparaître, et une carte perdrait son étiquette sans que rien ne
    # le signale.
    for conversation_id, comptes in (await _comptes_clivantes(session, ids)).items():
        repartition = resultat[conversation_id]
        repartition.n_clivantes = comptes[0] if comptes else None
        repartition.clivant = clivage.porte_etiquette(comptes)

    return resultat


async def _comptes_clivantes(
    session: AsyncSession, ids: list[int]
) -> dict[int, list[int | None]]:
    """Par conversation, le `n_clivantes` de ses derniers calculs aboutis.

    Du plus récent au plus ancien, et bornés à `clivage.CALCULS_POUR_ETIQUETTE` : c'est
    tout ce dont l'étiquette a besoin, et remonter l'historique entier ferait grossir la
    réponse avec l'âge du site pour un résultat inchangé.

    Une seule requête pour toute la liste, comme les trois autres — et non une par
    débat.
    """
    classement = (
        select(
            AnalysisRun.conversation_id.label("conversation_id"),
            AnalysisRun.n_clivantes.label("n_clivantes"),
            func.row_number()
            .over(
                partition_by=AnalysisRun.conversation_id,
                order_by=(AnalysisRun.finished_at.desc(), AnalysisRun.id.desc()),
            )
            .label("rang"),
        )
        .where(
            AnalysisRun.status == AnalysisStatus.ok,
            AnalysisRun.conversation_id.in_(ids),
        )
        .subquery()
    )
    lignes = (
        await session.execute(
            select(classement.c.conversation_id, classement.c.n_clivantes)
            .where(classement.c.rang <= clivage.CALCULS_POUR_ETIQUETTE)
            .order_by(classement.c.conversation_id, classement.c.rang)
        )
    ).all()

    comptes: dict[int, list[int | None]] = {}
    for conversation_id, n_clivantes in lignes:
        comptes.setdefault(conversation_id, []).append(n_clivantes)
    return comptes


# --- Ce sur quoi les groupes se rejoignent, tous débats confondus (chantier L6) ----

#: Combien de propositions un même débat peut placer dans la liste. La valeur du L3,
#: reprise telle quelle : c'est déjà « ce qu'un lecteur retient d'un débat ».
#:
#: **Sans ce plafond, un seul débat pourrait remplir l'écran.** Un débat mûr, très
#: consensuel et riche en propositions occuperait les dix lignes, et la vitrine du site
#: montrerait un débat au lieu de montrer le site. Le plafond est une règle éditoriale,
#: pas une limite technique : il s'enlève en une ligne si le client le préfère.
ACCORDS_PAR_DEBAT = resultats.N_CONSENSUELLES

#: Combien de propositions par débat entrent dans l'examen. Le classement par consensus
#: propose ; la règle (b) — tous les groupes du même côté — dispose, et elle en refuse
#: beaucoup : sur le débat du permis, deux des trois premières sont écartées. Regarder
#: les dix premières de chaque débat laisse donc largement de quoi en retenir trois,
#: tout en bornant le travail quel que soit le nombre de propositions du site.
CANDIDATS_PAR_DEBAT = 10


@dataclass
class Accord:
    """Une proposition sur laquelle les groupes d'un débat se rejoignent.

    **La direction est garantie, pas déduite** : `sens` n'est renseigné que si chaque
    groupe qui s'est exprimé penche de ce côté (`resultats.sens_affichable`). C'est ce
    qui autorise la liste à écrire « Les groupes l'approuvent » sans imprimer les barres
    à côté — la phrase ne peut pas être démentie, et le lien mène au détail.
    """

    texte: str
    sens: clivage.Sens
    #: Votes des participants CLASSÉS, tous groupes retenus confondus — la même
    #: définition qu'au tableau de fin de parcours.
    n_votes: int
    titre_debat: str
    slug_debat: str


async def accords_en_cours(
    session: AsyncSession, filtre: Filtre | None = None
) -> list[Accord]:
    """Les propositions les plus consensuelles des débats OUVERTS, tous débats confondus.

    « En cours » se prend au pied de la lettre, comme pour le compteur de propositions
    ouvertes : une consultation close n'est plus un débat en cours, et ce que ses
    groupes ont fini par accepter n'appartient plus à l'actualité du site.

    **Quatre requêtes, quel que soit le nombre de débats.** Une par question — les
    calculs, les groupes, les propositions candidates, les votes —, et aucune à
    l'intérieur d'une boucle. Une version qui aurait appelé `resultats.par_proposition`
    débat par débat aurait été plus courte et aurait tenu tant que le catalogue est
    petit : c'est exactement le N+1 que le G3 décrit et que ce fichier évite ailleurs.

    Le classement est **global** — la proposition la plus consensuelle du site vient en
    tête, quel que soit son débat — mais un même débat n'en place que `ACCORDS_PAR_DEBAT`.
    La liste n'est donc pas tout à fait « les N plus consensuelles du site » : c'est un
    écart assumé, en faveur de la variété, et il est dit à l'écran.
    """
    dernier = _dernier_calcul_par_conversation()
    debats = (
        await session.execute(
            appliquer_filtre(
                select(
                    Conversation.id,
                    Conversation.slug,
                    Conversation.title,
                    dernier.c.run_id,
                ).join(dernier, dernier.c.conversation_id == Conversation.id),
                filtre or Filtre(),
            ).where(
                Conversation.is_public.is_(True),
                Conversation.state == ConversationState.open,
                Conversation.moderation_status == ModerationStatus.approved,
            )
        )
    ).all()
    if not debats:
        return []

    calculs = {run_id: (slug, titre) for _, slug, titre, run_id in debats}

    groupes = await _groupes_retenus(session, list(calculs))
    candidats = await _candidats_consensuels(session, list(calculs))
    votes = await _votes_des_candidats(session, dernier, [c[1] for c in candidats])

    retenus: list[Accord] = []
    comptes: dict[int, int] = {}
    for run_id, statement_id, texte, accord_brut, desaccord_brut in candidats:
        noms = groupes.get(run_id)
        # Aucun groupe assez grand : le débat n'a rien à dire, comme au tableau de fin
        # de parcours, qui ne donne pas de colonne à un groupe d'une seule personne.
        if not noms:
            continue
        if comptes.get(run_id, 0) >= ACCORDS_PAR_DEBAT:
            continue
        parts = []
        for stable, nom in noms:
            valeurs = votes.get((statement_id, stable), {})
            accord = valeurs.get(1, 0)
            desaccord = valeurs.get(-1, 0)
            total = accord + desaccord + valeurs.get(0, 0)
            pour, contre, passe = resultats.pourcentages(accord, desaccord, total)
            parts.append(
                resultats.PartGroupe(
                    nom=nom,
                    n_votes=total,
                    accord=pour,
                    desaccord=contre,
                    passe=passe,
                )
            )
        ligne = resultats.LigneProposition(
            texte=texte,
            n_votes=sum(part.n_votes for part in parts),
            groupes=parts,
        )
        # La règle (b), appelée et non réécrite : la même proposition doit recevoir le
        # même verdict ici et sur la page du débat.
        sens = resultats.sens_affichable(
            clivage.sens_de(accord_brut, desaccord_brut), ligne
        )
        if sens is None:
            continue
        slug, titre = calculs[run_id]
        retenus.append(
            Accord(
                texte=texte,
                sens=sens,
                n_votes=ligne.n_votes,
                titre_debat=titre,
                slug_debat=slug,
            )
        )
        comptes[run_id] = comptes.get(run_id, 0) + 1
    return retenus


async def _groupes_retenus(
    session: AsyncSession, run_ids: list[int]
) -> dict[int, list[tuple[int, str]]]:
    """Par calcul, les groupes assez grands pour compter, dans l'ordre de leur identité.

    Même seuil qu'au tableau de fin de parcours : un groupe d'une seule personne est un
    artefact du découpage, pas une opinion partagée. Le compter ferait dire « tous les
    groupes l'approuvent » à une phrase dont un des « groupes » serait quelqu'un.
    """
    effectifs = (
        await session.execute(
            select(
                ParticipantProjection.run_id,
                ParticipantProjection.stable_group_id,
                func.count(ParticipantProjection.id),
            )
            .where(
                ParticipantProjection.run_id.in_(run_ids),
                ParticipantProjection.stable_group_id.isnot(None),
            )
            .group_by(
                ParticipantProjection.run_id, ParticipantProjection.stable_group_id
            )
        )
    ).all()

    retenus: dict[int, list[tuple[int, str]]] = {}
    for run_id, stable, taille in effectifs:
        if taille >= MIN_GROUP_SIZE:
            retenus.setdefault(run_id, []).append((stable, group_name(stable) or "?"))
    for lignes in retenus.values():
        lignes.sort()
    return retenus


async def _candidats_consensuels(session: AsyncSession, run_ids: list[int]):
    """Les propositions les mieux classées de chaque calcul, dans l'ordre global.

    On lit `clivage`, **jamais les deux bruts** pour en refaire un score : c'est la
    règle posée au L2, et la contourner annulerait le plancher qui empêche les petits
    nombres de décider. Les bruts remontent tout de même, parce que `clivage.sens_de` a
    besoin d'eux pour dire de quel côté penche un score DÉJÀ accordé — ce qui est autre
    chose que le recalculer.

    L'identifiant départage les scores égaux : sans lui, deux propositions au même
    clivage pourraient s'échanger d'une requête à l'autre, et la pagination de la liste
    en sauterait une.
    """
    classement = (
        select(
            StatementStat.run_id.label("run_id"),
            StatementStat.statement_id.label("statement_id"),
            StatementStat.consensus_accord.label("consensus_accord"),
            StatementStat.consensus_desaccord.label("consensus_desaccord"),
            StatementStat.clivage.label("clivage"),
            func.row_number()
            .over(
                partition_by=StatementStat.run_id,
                order_by=(StatementStat.clivage, StatementStat.statement_id),
            )
            .label("rang"),
        )
        .where(
            StatementStat.run_id.in_(run_ids),
            StatementStat.group_id.is_(None),
            StatementStat.clivage.isnot(None),
        )
        .subquery()
    )
    return (
        await session.execute(
            select(
                classement.c.run_id,
                classement.c.statement_id,
                Statement.text,
                classement.c.consensus_accord,
                classement.c.consensus_desaccord,
            )
            .join(Statement, Statement.id == classement.c.statement_id)
            # Une proposition rejetée par la modération APRÈS le calcul a gardé sa
            # statistique et perdu son droit de paraître.
            .where(
                classement.c.rang <= CANDIDATS_PAR_DEBAT,
                Statement.moderation_status == ModerationStatus.approved,
            )
            .order_by(classement.c.clivage, classement.c.statement_id)
        )
    ).all()


async def _votes_des_candidats(session: AsyncSession, dernier, statement_ids: list[int]):
    """Les votes des participants classés, par proposition, groupe et valeur.

    La jointure passe par le calcul de LA conversation de la proposition, et non par
    « un calcul quelconque » : quelqu'un qui participe à deux débats est projeté dans
    deux calculs, et une jointure lâche compterait son vote sous les deux identités de
    groupe qu'il porte — une dans chaque débat.
    """
    if not statement_ids:
        return {}
    comptes = (
        await session.execute(
            select(
                Vote.statement_id,
                ParticipantProjection.stable_group_id,
                Vote.value,
                func.count(Vote.id),
            )
            .join(Statement, Statement.id == Vote.statement_id)
            .join(dernier, dernier.c.conversation_id == Statement.conversation_id)
            .join(
                ParticipantProjection,
                (ParticipantProjection.participant_id == Vote.participant_id)
                & (ParticipantProjection.run_id == dernier.c.run_id),
            )
            .where(
                Vote.statement_id.in_(statement_ids),
                ParticipantProjection.stable_group_id.isnot(None),
            )
            .group_by(
                Vote.statement_id,
                ParticipantProjection.stable_group_id,
                Vote.value,
            )
        )
    ).all()

    brut: dict[tuple[int, int], dict[int, int]] = {}
    for statement_id, stable, valeur, compte in comptes:
        brut.setdefault((statement_id, stable), {})[valeur] = compte
    return brut
