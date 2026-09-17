"""Schémas d'entrée/sortie de l'API."""

import uuid
from datetime import datetime

from fastapi_users import schemas
from pydantic import BaseModel


class UserRead(schemas.BaseUser[uuid.UUID]):
    display_name: str | None = None
    bio: str | None = None


class UserCreate(schemas.BaseUserCreate):
    display_name: str | None = None
    bio: str | None = None


class UserUpdate(schemas.BaseUserUpdate):
    display_name: str | None = None
    bio: str | None = None


class AccountInfo(BaseModel):
    email: str
    is_verified: bool
    display_name: str | None = None
    bio: str | None = None


class MeResponse(BaseModel):
    """Identité courante. `participant_id` est toujours présent — avec ou sans compte."""

    participant_id: int
    authenticated: bool
    account: AccountInfo | None = None
    #: Niveaux et badges appartiennent au compte : None pour un visiteur anonyme.
    progress: "ProgressRead | None" = None


class SourceRead(BaseModel):
    """Un lien tel qu'il est SERVI : l'adresse, et le domaine qu'on affichera.

    `domaine` est calculé par le serveur à chaque réponse et jamais stocké. Le
    navigateur ne le déduit pas de l'adresse lui-même : la seule règle qui compte est
    « le nom montré est celui vers lequel le lien mène », et la tenir à un seul endroit
    est ce qui la rend vraie partout.
    """

    url: str
    domaine: str
    label: str | None = None
    #: La date, en toutes lettres, depuis laquelle la page est constatée disparue —
    #: `None` tant qu'il n'y a rien à dire (chantier K). Une seule information plutôt
    #: qu'un drapeau et une date, qui pourraient se contredire : ce qui est daté est
    #: signalé, et ce qui n'est pas daté ne l'est pas.
    #:
    #: Le lien reste servi et **reste cliquable** : le lecteur peut vouloir essayer
    #: quand même, ou chercher une copie archivée. On informe, on ne retire pas.
    signale_le: str | None = None


class SourceCreate(BaseModel):
    url: str
    label: str | None = None


class StatementRead(BaseModel):
    id: int
    text: str
    is_seed: bool
    #: Cette proposition a-t-elle été écrite par un PARTICIPANT ? Le modèle distingue
    #: trois origines depuis l'origine — amorce (`is_seed`), apport d'un participant
    #: (`author_participant_id` renseigné), et ajout de la modération en cours de route
    #: (ni l'un ni l'autre) — mais seule la première voyageait jusqu'ici.
    #:
    #: Exposée au chantier I2 (instruction 10) pour la ligne de méta de la carte de
    #: vote, qui attribue la proposition. Sans elle, l'interface aurait dû traiter
    #: « pas une amorce » comme « écrite par un participant », ce qui est faux du
    #: troisième cas — et attribuer à un participant un texte de la modération est
    #: exactement le genre d'erreur qu'une plateforme de débat ne peut pas se
    #: permettre.
    par_un_participant: bool = False
    #: Les liens de l'auteur, dans l'ordre où il les a posés. Vide le plus souvent.
    sources: list[SourceRead] = []
    #: La date, en toutes lettres, à laquelle l'auteur a accepté une reformulation de sa
    #: proposition — `None` tant qu'il n'y a rien à dire (MOD-17). Même forme et même
    #: raison que `signale_le` : ce qui est daté a eu lieu, ce qui n'est pas daté n'a pas
    #: eu lieu, et un drapeau séparé pourrait contredire la date.
    #:
    #: **Le vote, lui, n'est averti de rien et ne bouge pas.** dom a tranché le
    #: 15 septembre 2026 : les votes portés sur une proposition reformulée restent
    #: valables comme si rien n'avait changé. Ce champ dit au LECTEUR que le texte a
    #: bougé ; il ne remet aucun vote en cause et n'en redemande aucun.
    #:
    #: **Le texte d'avant n'est PAS servi**, et c'est une décision de dom du 15 septembre
    #: 2026, revenue sur la sienne du même soir : « on ne montre pas la première version,
    #: on ne garde que la reformulation ». Elle rejoint le §6.1 du plan v2 — l'auteur
    #: ayant validé lui-même la reformulation, exhiber l'ancienne reviendrait à montrer
    #: un brouillon qu'il a écarté.
    #:
    #: Il reste **conservé en base et consultable par le responsable** (registre du
    #: MOD-16, journal d'audit) : c'est ce qui permet de montrer ce qui s'est passé le
    #: jour où quelqu'un conteste. Non consultable **publiquement**, ce n'est pas la même
    #: chose qu'effacé.
    reformulee_le: str | None = None


def statement_read(
    statement,
    signales: dict[str, object] | None = None,
    reformulees: dict[int, object] | None = None,
) -> StatementRead:
    """Rend une proposition telle qu'elle est servie, liens compris.

    Un seul endroit construit cette réponse, parce qu'une seule règle décide de ce qui
    est montré : **un lien dont on ne sait pas nommer la destination n'est pas
    affiché**. Écrite deux fois, elle finirait appliquée à un endroit et pas à l'autre,
    et c'est la page de vote — celle que tout le monde voit — qui perdrait.

    `signales` vient de `liens_verification.signalements()` et vaut `None` par défaut :
    un appelant qui ne s'en occupe pas sert des liens sans mention, jamais une mention
    fausse. C'est le sens que doit avoir un oubli.

    `reformulees` suit exactement la même convention, et vient de
    `reformulation.acceptees_par_proposition()`. Un oubli sert une proposition sans
    mention de reformulation — jamais une proposition qu'on annoncerait reformulée à
    tort, ce qui serait bien pire que le silence.
    """
    from app.services.chiffres import en_toutes_lettres

    signales = signales or {}
    reformulees = reformulees or {}
    sources = []
    for source in statement.sources:
        domaine = source.domaine
        if domaine is None:
            continue
        quand = signales.get(source.url)
        sources.append(
            SourceRead(
                url=source.url,
                domaine=domaine,
                label=source.label,
                signale_le=en_toutes_lettres(quand.date()) if quand else None,
            )
        )
    reformulee = reformulees.get(statement.id)
    return StatementRead(
        id=statement.id,
        text=statement.text,
        is_seed=statement.is_seed,
        par_un_participant=statement.author_participant_id is not None,
        sources=sources,
        reformulee_le=(
            en_toutes_lettres(reformulee.repondu_le.date())
            if reformulee is not None and reformulee.repondu_le is not None
            else None
        ),
    )


class ConversationSummary(BaseModel):
    slug: str
    title: str
    description: str | None = None
    state: str


class ConversationDetail(ConversationSummary):
    allow_participant_statements: bool
    statements: list[StatementRead]


class StatementCreate(BaseModel):
    text: str
    #: Deux au plus, et l'API le refuse au-delà plutôt que de tronquer en silence.
    sources: list[SourceCreate] = []


class StatementSubmitted(BaseModel):
    """Réponse à une proposition.

    `visible` dit sans ambiguïté si la proposition est déjà exposée aux autres :
    c'est la différence entre pré- et post-modération, et le participant doit la
    connaître pour ne pas croire sa proposition perdue.
    """

    id: int
    moderation_status: str
    visible: bool


class VoteCreate(BaseModel):
    statement_id: int
    #: +1 d'accord, -1 pas d'accord, 0 passer. Convention red-dwarf (cf. models/vote.py).
    value: int


class ResultatImmediat(BaseModel):
    """Ce que les autres ont répondu sur la proposition qu'on vient de voter.

    Les trois pourcentages viennent de `resultats.pourcentages` et somment donc
    toujours à 100 — la même fonction que la page de résultats, pour que deux écrans
    n'arrondissent pas différemment la même proposition.
    """

    n_accord: int
    n_desaccord: int
    n_passe: int
    #: Le total, qui n'est pas la somme des trois pourcentages mais celle des comptes.
    #: Affiché tel quel : « 12 votes » à côté d'un « 75 % » dit tout de suite ce que
    #: vaut ce 75 %.
    total: int
    part_accord: int
    part_desaccord: int
    part_passe: int


class Parcours(BaseModel):
    """Ce qu'une personne a répondu sur un débat. Le total est la somme des trois."""

    accord: int
    desaccord: int
    passe: int
    total: int


class ValidationProposee(BaseModel):
    """La carte de validation servie après un vote, une fois toutes les sept (MOD-4).

    **Elle voyage dans la réponse du vote, et non derrière une route à elle.** Une route
    `GET /api/validations/suivante` serait tirable à volonté : il suffirait de la
    rappeler jusqu'à tomber sur la proposition qu'on veut faire retirer, et le tirage au
    sort cesserait d'en être un. La carte arrive quand le site la donne.

    Le texte est recopié ici plutôt que d'être rechargé par le navigateur : la
    proposition est publique, elle est déjà servie mille fois par jour sur ce même débat,
    et un second aller-retour pour l'obtenir n'apporterait rien qu'une latence au milieu
    d'un parcours de vote.

    **Ce qu'elle ne porte pas** : ni le nombre de validations déjà rendues sur cette
    proposition, ni ce que les autres ont répondu, ni le rang de la sollicitation dans le
    parcours. Chacun de ces chiffres apprendrait quelque chose sur l'état du dossier — et
    c'est la règle du §4 du cadrage de la modération, qui vaut ici comme ailleurs.
    """

    statement_id: int
    texte: str
    #: Les phrases de la carte, écrites par le serveur. Elles sont dans
    #: `app/services/validation.py`, à côté de la règle qu'elles décrivent : un gabarit
    #: recopié dans un template est un gabarit qui finira par annoncer un effet que le
    #: code ne produit plus.
    titre: str
    consigne: str
    libelle_conforme: str
    libelle_a_revoir: str


class VoteRecorded(BaseModel):
    #: Code du badge décerné par CE vote, le cas échéant — permet à l'interface de
    #: l'annoncer au bon moment plutôt qu'au rechargement suivant.
    badge_awarded: str | None = None
    #: Le badge complet (intitulé, icône), pour que l'interface n'ait pas à tenir
    #: sa propre table de correspondance des codes.
    badge: "BadgeRead | None" = None
    participant_id: int
    statement_id: int
    value: int
    #: False = re-vote, la ligne existait déjà et a été mise à jour.
    created: bool
    remaining: int
    #: La répartition des votes sur CETTE proposition, ce vote-ci compris.
    resultat: "ResultatImmediat | None" = None
    #: De quoi tenir la barre de progression vers le déblocage de la carte, sans
    #: recharger la page : le seuil de CE débat (variable, jamais un 7 fixe) et le
    #: nombre de votes émis après celui qu'on vient d'enregistrer.
    seuil_situe: int | None = None
    votes_emis: int | None = None
    #: Le parcours de la personne sur ce débat, ce vote-ci compris — accord, désaccord,
    #: passe. Voyage ici pour que le panneau « Votre parcours » se mette à jour au fil
    #: des votes plutôt qu'au rechargement suivant.
    parcours: "Parcours | None" = None
    #: La carte de validation, quand ce vote-ci en déclenche une (MOD-4). `None` le reste
    #: du temps, c'est-à-dire six fois sur sept — et aussi quand la personne a atteint son
    #: plafond du jour, ou que le débat n'a plus rien à lui faire regarder. Le navigateur
    #: n'a donc rien à calculer : il montre la carte si elle est là.
    validation: "ValidationProposee | None" = None


class GroupeAnnonce(BaseModel):
    """Le groupe d'une personne, tel qu'on le lui annonce en fin de parcours.

    `nom` est None quand le dernier calcul abouti ne la situe pas ; `raison` dit alors
    pourquoi, et il y en a toujours une — un silence laisserait croire à une panne.
    """

    nom: str | None = None
    #: Autres membres du groupe, la personne exclue.
    autres: int = 0
    raison: str | None = None
    #: Instant du prochain calcul des groupes (chantier G9), pour quelqu'un qui n'est
    #: pas encore situé. Une DATE et non une durée : la page reste ouverte après le
    #: dernier vote, et « dans 12 minutes » figé dans la réponse deviendrait faux au
    #: bout de treize. Le navigateur en tire le temps restant, et le rafraîchit.
    #:
    #: Nul quand l'attente ne changerait rien : la personne connaît déjà son groupe, ou
    #: il lui manque des votes — attendre le prochain calcul ne la situerait pas
    #: davantage, c'est voter qui la débloque.
    prochain_calcul: datetime | None = None


class NextStatement(BaseModel):
    participant_id: int
    remaining: int
    #: None quand le participant a tout vu.
    statement: StatementRead | None = None
    #: Renseigné UNIQUEMENT sur l'appel qui clôt le parcours (`statement` à None).
    #:
    #: Il voyage dans cette réponse-là plutôt que dans une route à lui : le G5 avait
    #: déjà tranché qu'on ne fait pas attendre l'écran le plus attendu de la visite
    #: pour un appel de plus. La requête ne coûte donc rien pendant le vote — elle ne
    #: part qu'une fois, à la toute fin.
    groupe: GroupeAnnonce | None = None


class BadgeRead(BaseModel):
    code: str
    label: str
    description: str
    icon: str


class ProgressRead(BaseModel):
    votes: int
    conversations: int
    points: int
    level: int
    label: str
    next_level_at: int | None = None
    points_to_next: int | None = None
    badges: list[BadgeRead] = []


class ConversationProposal(BaseModel):
    title: str
    description: str | None = None
    statements: list[str]


class PositionRead(BaseModel):
    """Un point de la carte. Volontairement SANS identifiant de participant : la carte
    est une nuée anonyme, pas un fichier d'opinions individuelles."""

    x: float
    y: float
    groupe: str | None = None


class EnveloppeRead(BaseModel):
    """Un groupe et son contour. `sommets` est absent sous 3 points — ce n'est pas une
    erreur, deux points ne délimitent pas une surface."""

    name: str
    size: int
    sommets: list[list[float]] | None = None


class VisiteurRead(BaseModel):
    """Où se situe la personne qui regarde — ou pourquoi elle ne se situe pas.

    `etat` vaut « situe », « sous_le_seuil » ou « en_attente_de_calcul ». Les deux
    derniers ne se confondent pas : l'un demande de voter davantage, l'autre seulement
    d'attendre le prochain calcul. Une position n'est jamais renvoyée hors de « situe ».
    """

    etat: str
    raison: str | None = None
    x: float | None = None
    y: float | None = None
    groupe: str | None = None


class CarteRead(BaseModel):
    """Carte d'un SEUL calcul. `run_id` est exposé pour que la contrainte soit
    vérifiable : deux cartes de runs différents ne se superposent pas."""

    run_id: int
    computed_at: str | None = None
    positions: list[PositionRead] = []
    groupes: list[EnveloppeRead] = []
    visiteur: VisiteurRead | None = None


class GroupRead(BaseModel):
    """Groupe d'opinion tel qu'affiché. `name` est l'identité STABLE (A, B, C…),
    jamais l'étiquette brute de k-means, qui change à chaque recalcul."""

    name: str | None = None
    others: int = 0
    total_participants: int = 0
    group_count: int = 0
    computed_at: str | None = None
    reason: str | None = None
