"""Conversation et propositions.

Une conversation regroupe des propositions (« statements ») sur lesquelles les
participants votent. Deux origines pour une proposition :

  - les propositions d'amorce, écrites par le modérateur à la création ;
  - les propositions des participants, soumises en cours de route.

Les secondes passent par la modération. Le mode par défaut est la PRÉ-modération
(invisible tant que non approuvée), l'inverse du défaut de Pol.is : sur un site
politique public c'est plus sûr, et `moderation_mode` le rend réglable par
conversation, donc la décision reste réversible.

Le chantier J y ajoute trois attaches, qui n'entrent dans aucun calcul :

  - `statement_source` — les liens de l'auteur d'une proposition, deux au plus ;
  - `conversation_source` — les chiffres publics saisis par un modérateur ;
  - `conversation_theme` — les thèmes du débat, un à trois.

Les deux premières ne sont pas fondues en une seule table alors qu'elles portent
toutes deux une URL : elles n'ont ni le même auteur, ni le même rythme, ni le même
risque, ni la même durée de vie — le lien meurt avec sa proposition, le chiffre
survit à toutes. Les réunir demanderait une colonne `type` et des colonnes nulles la
moitié du temps.
"""

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ConversationState(str, enum.Enum):
    draft = "draft"      # en préparation, invisible des participants
    open = "open"        # ouverte au vote
    closed = "closed"    # consultable, plus de vote ni de proposition


class ModerationMode(str, enum.Enum):
    pre = "pre"          # une proposition est invisible tant qu'elle n'est pas approuvée
    post = "post"        # visible d'emblée, retirée si rejetée


class ModerationStatus(str, enum.Enum):
    """Où en est la publication d'une conversation, d'une proposition ou d'un nom.

    `retire` est ajouté par le chantier Modération (MOD-3a) : c'est le **retrait
    conservatoire** d'une proposition publiée, déclenché par le premier signalement de
    ligne rouge. La liste existante est ÉTENDUE plutôt que doublée d'une seconde — deux
    énumérations parallèles finiraient par diverger, et surtout tout le site filtre déjà
    sur `== approved`, jamais sur `!= rejected` : une valeur de plus sort donc la
    proposition de l'affichage, du tirage pondéré et des API publiques **sans qu'aucune
    requête soit à reprendre**. C'est ce qui rend le retrait sûr, et c'est la raison pour
    laquelle il n'a pas fallu de colonne « masqué » à côté.

    Le type PostgreSQL `moderation_status` est partagé avec `conversation` et
    `group_naming`, qui gagnent donc eux aussi la valeur. Aucun des deux ne l'emploie, et
    rien ne l'y écrit : retirer un débat entier n'est pas la même décision que retirer
    une proposition, et se fera avec ses propres gestes le jour où on la prendra.
    """

    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    retire = "retire"


class Conversation(Base):
    __tablename__ = "conversation"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    state: Mapped[ConversationState] = mapped_column(
        Enum(ConversationState, name="conversation_state"),
        default=ConversationState.draft,
        nullable=False,
    )
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    allow_participant_statements: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    moderation_mode: Mapped[ModerationMode] = mapped_column(
        Enum(ModerationMode, name="moderation_mode"),
        default=ModerationMode.pre,
        nullable=False,
    )
    # Une conversation proposée par un participant est `pending` : elle n'existe pas
    # publiquement tant qu'un modérateur ne l'a pas approuvée. Une conversation créée
    # par un modérateur naît `approved` — il est la modération.
    moderation_status: Mapped[ModerationStatus] = mapped_column(
        Enum(ModerationStatus, name="moderation_status"),
        default=ModerationStatus.approved,
        nullable=False,
        index=True,
    )
    proposed_by_participant_id: Mapped[int | None] = mapped_column(
        ForeignKey("participant.id", ondelete="SET NULL"), nullable=True
    )
    # Fige le nombre de groupes de l'analyse (C6). NULL = k choisi automatiquement.
    # Prévu dès maintenant parce que le chantier B a montré que le k automatique de
    # red-dwarf varie d'un recalcul à l'autre.
    force_group_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Nombre de votes exigé d'un participant pour entrer dans l'analyse.
    # NULL = borné automatiquement par le nombre de propositions (voir
    # app/analysis/thresholds.py) ; une valeur explicite l'emporte.
    min_user_votes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- couche intermédiaire du chantier D (option C') ---------------------------
    # Découpage groupe du dernier calcul : étiquette brute -> participants. Sert de
    # point de départ au tour suivant, recentré sur les positions du moment. C'est
    # un cache, comme `base_cluster` : NULL = le prochain tour part à froid.
    group_partition: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Les trois compteurs du lisseur de k (app/analysis/smoothing.py).
    smoother_last_k: Mapped[int | None] = mapped_column(Integer, nullable=True)
    smoother_last_k_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    smoother_smoothed_k: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- vidéo de présentation (chantier Vidéo, VIDEO-1) --------------------------
    # Quatre colonnes, toutes nullables : un débat sans vidéo est le cas normal, et le
    # restera. **Aucun octet de vidéo n'entre ici** — ce sont des chemins RELATIFS à
    # `settings.video_racine`, sur le modèle des liens-source du chantier J qui stockent
    # une adresse et non une page. Le binaire vit sur le disque et sera servi par nginx.
    #
    # `video_duree_secondes` est la durée MESURÉE par ffprobe à la réception, jamais
    # celle qu'un navigateur aurait annoncée. `video_profil` note avec quel jeu de
    # réglages le fichier a été encodé : le jour où le débit change, c'est ce qui dira
    # laquelle des vidéos déjà en ligne date d'avant, sans sonder le disque entier.
    video_chemin: Mapped[str | None] = mapped_column(String(255), nullable=True)
    video_miniature_chemin: Mapped[str | None] = mapped_column(String(255), nullable=True)
    video_duree_secondes: Mapped[float | None] = mapped_column(Float, nullable=True)
    video_profil: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    statements: Mapped[list["Statement"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Statement.id",
    )
    themes: Mapped[list["ConversationTheme"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    # Volontairement PARESSEUSE, contrairement à `themes` : l'accueil charge cinq
    # conversations et la page « Débats » dix, et aucune des deux n'a que faire des
    # chiffres. Seule la page qui les affiche les lit, et elle passe par
    # `app/services/chiffres.py` plutôt que par cette relation.
    sources: Mapped[list["ConversationSource"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationSource.position",
    )

    @property
    def codes_themes(self) -> list[str]:
        """Les codes du débat, dans l'ordre de la liste et non celui de la saisie.

        Deux débats portant les mêmes thèmes doivent afficher leurs étiquettes dans le
        même ordre, sinon les comparer à l'œil sur une liste devient impossible.
        """
        from app.services.themes import themes_connus

        return themes_connus([lien.code for lien in self.themes])

    @property
    def libelles_themes(self) -> list[str]:
        """Les libellés à afficher, dans le même ordre que `codes_themes`.

        Passe par la constante : un code retiré de la liste s'affiche alors sous son
        code plutôt que de faire disparaître l'étiquette — un débat étiqueté l'an
        dernier reste étiqueté.
        """
        from app.services.themes import libelle

        return [libelle(code) for code in self.codes_themes]

    def __repr__(self) -> str:
        return f"<Conversation {self.slug}>"


class Statement(Base):
    __tablename__ = "statement"
    __table_args__ = (
        # Un double clic sur « proposer » créait autant de propositions que de
        # requêtes. La règle est portée par la contrainte, pas par une
        # vérification applicative qui laisserait passer les cas concurrents.
        UniqueConstraint("conversation_id", "text", name="uq_statement_text"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)

    # NULL pour une proposition d'amorce écrite par le modérateur.
    author_participant_id: Mapped[int | None] = mapped_column(
        ForeignKey("participant.id", ondelete="SET NULL"), nullable=True
    )
    is_seed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Proposition « méta » (question de cadrage) : traitée à part par l'analyse (C6).
    is_meta: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    moderation_status: Mapped[ModerationStatus] = mapped_column(
        Enum(ModerationStatus, name="moderation_status"),
        default=ModerationStatus.pending,
        nullable=False,
        index=True,
    )
    moderated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    moderated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- retrait conservatoire (chantier Modération, MOD-3a) ----------------------
    # Trois colonnes nullables. Le statut `retire` suffit à sortir la proposition de la
    # circulation ; celles-ci disent QUAND, POURQUOI et PAR QUI — ce qu'un statut ne dit
    # pas, et ce qu'il faudra produire le jour où l'auteur conteste.
    #
    # **Rien n'est supprimé en base, jamais.** Une proposition retirée n'est plus
    # affichée, ne sort plus du tirage pondéré et n'apparaît dans aucune API publique,
    # mais ses votes déjà émis restent et restent comptés dans les groupes d'opinion :
    # on ne réécrit pas la mesure du passé.
    retire_le: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Le code du motif qui a DÉCIDÉ du retrait, tel qu'il était au moment du retrait.
    retire_motif: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: `systeme` pour le retrait automatique de ligne rouge, l'adresse du compte sinon.
    #: Une chaîne et non une clé étrangère vers `user` : le premier retrait est TOUJOURS
    #: l'œuvre du système, et une clé étrangère serait nulle là où l'information compte.
    retire_par: Mapped[str | None] = mapped_column(String(160), nullable=True)
    #: Le jeton de la voie de recours (MOD-3b), posé au moment du retrait et jamais
    #: réémis. **C'est le seul droit d'accès à `/contester/<jeton>`**, et il ne peut pas
    #: en être autrement : l'auteur d'une proposition est anonyme dans 98 % des cas, il
    #: n'y a donc aucun compte sur lequel adosser une autorisation. Un jeton de 32 octets
    #: tirés de `secrets` est ce qui remplace le compte, et il vaut ce que vaut sa
    #: longueur — d'où l'index unique, et d'où le fait qu'il ne soit jamais journalisé.
    #:
    #: NULL tant que la proposition n'a pas été retirée : il n'y a rien à contester.
    jeton_contestation: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    #: Quand l'auteur a fermé le rappel concernant CETTE proposition (MOD-6, repli).
    #: NULL = jamais fermé, donc le rappel est dû — et c'est le bon défaut : une
    #: proposition retirée avant ce lot en reçoit un, son auteur n'ayant jamais été
    #: prévenu. Par proposition et non par personne : chaque retrait est une décision
    #: distincte, et son exposé des motifs est dû pour lui-même.
    rappel_ferme_le: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped["Conversation"] = relationship(back_populates="statements")
    # `selectin` : chargé avec la proposition, en UNE requête pour toute la fournée.
    # En asynchrone un chargement différé lèverait MissingGreenlet à l'accès, et
    # l'analyse ne paie rien puisqu'elle ne lit que `Statement.id`.
    sources: Mapped[list["StatementSource"]] = relationship(
        back_populates="statement",
        cascade="all, delete-orphan",
        order_by="StatementSource.position",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Statement {self.id} {self.moderation_status.value}>"


class StatementSource(Base):
    """Un lien attaché à une proposition par son auteur. Deux au plus.

    Le lien est du **contenu publié**, pas une métadonnée : il voyage avec la
    proposition, passe par la même modération qu'elle et disparaît avec elle. D'où la
    cascade, et d'où l'absence de tout statut de modération propre — un seul geste,
    une seule décision.

    Ce qui est stocké est ce que l'auteur a écrit. Le domaine affiché au lecteur est
    recalculé à partir de `url` (`app/services/liens.py`) et jamais lu ici : une
    colonne `domaine` pourrait diverger de l'adresse après une correction, et le
    lecteur verrait alors un nom qui n'est plus celui vers lequel il va.
    """

    __tablename__ = "statement_source"
    __table_args__ = (
        # Le plafond de deux liens est porté par la BASE et non par le seul contrôle
        # de saisie : la contrainte tient aussi pour l'import, la console et le
        # back-office, qui ne passent pas par le formulaire.
        CheckConstraint("position in (1, 2)", name="ck_statement_source_position"),
        UniqueConstraint("statement_id", "position", name="uq_statement_source_position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("statement.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    # Le libellé écrit par l'auteur, jamais récupéré chez un tiers. NULL = il n'en a
    # pas écrit, et c'est le domaine seul qui sera montré.
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)

    statement: Mapped["Statement"] = relationship(back_populates="sources")

    @property
    def domaine(self) -> str | None:
        """Le domaine à montrer, recalculé à chaque affichage.

        Propriété et non colonne : une valeur figée pourrait diverger de l'adresse
        après une correction, et le lecteur verrait alors un nom qui n'est plus celui
        vers lequel il va. None quand l'adresse est illisible — la vue n'affiche alors
        pas le lien du tout, plutôt que de le montrer sans dire où il mène.
        """
        from app.services.liens import domaine_de

        return domaine_de(self.url)

    def __repr__(self) -> str:
        return f"<StatementSource {self.id} #{self.position}>"


class ConversationSource(Base):
    """Un chiffre public d'un débat, saisi à la main par un modérateur.

    Aucune récupération automatique : ni data.gouv.fr, ni l'API de l'Insee. Une API
    publique change de schéma sans prévenir, et le jour où elle le fait, un site
    politique publie un chiffre faux avec une source officielle en dessous — le pire
    des deux mondes. Un modérateur qui recopie un chiffre le lit ; un script ne lit
    rien.

    **Deux dates et non une.** « Mis à jour le 5 septembre » ne dit pas si la donnée
    est de 2026 ou de 2019, et c'est exactement la confusion qu'une page de chiffres
    doit empêcher. `date_donnees` dit à quand se rapporte le chiffre, `date_verification`
    quand un humain l'a regardé pour la dernière fois.
    """

    __tablename__ = "conversation_source"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Ordre d'affichage, tenu par le modérateur. Non unique à dessein : réordonner en
    #: bloc sous une contrainte d'unicité demanderait un passage par des valeurs
    #: temporaires pour un écran qui sert à une personne.
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    titre: Mapped[str] = mapped_column(String(300), nullable=False)
    #: Le chiffre EN TEXTE : « 12,4 % », « 3 200 communes », « 1,8 million ». Un
    #: numérique obligerait à stocker l'unité à côté et à reconstruire la mise en forme
    #: à l'affichage, pour une donnée que personne n'additionne jamais.
    valeur: Mapped[str] = mapped_column(String(120), nullable=False)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)

    date_donnees: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_verification: Mapped[date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped["Conversation"] = relationship(back_populates="sources")

    @property
    def domaine(self) -> str | None:
        """Le domaine à montrer, recalculé à chaque affichage — comme pour un lien de
        participant, et pour la même raison : une valeur figée pourrait diverger de
        l'adresse après une correction."""
        from app.services.liens import domaine_de

        return domaine_de(self.url)

    def __repr__(self) -> str:
        return f"<ConversationSource {self.id} {self.titre[:30]!r}>"


class ConversationTheme(Base):
    """Un thème posé sur un débat. Un débat en porte 1 à 3.

    Table de liaison et non colonne-tableau sur `conversation` : le filtre de l'accueil
    demande « les débats portant l'un de ces codes », ce qu'une jointure indexée fait
    directement.

    Le `code` n'a pas de clé étrangère, et c'est délibéré : la liste des thèmes vit
    comme constante Python (`app/services/themes.py`), pas comme table. Un code retiré
    de la constante ne casse donc rien — il cesse d'être proposé et d'être affiché,
    et les lignes qui le portent restent.
    """

    __tablename__ = "conversation_theme"
    __table_args__ = (
        UniqueConstraint("conversation_id", "code", name="uq_conversation_theme"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    conversation: Mapped["Conversation"] = relationship(back_populates="themes")

    def __repr__(self) -> str:
        return f"<ConversationTheme {self.conversation_id} {self.code}>"
