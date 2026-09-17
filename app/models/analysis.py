"""Résultats d'analyse red-dwarf.

Un enregistrement par exécution plutôt qu'un écrasement : l'interface lit toujours le
dernier `run` en statut `ok`, si bien qu'un recalcul en cours ou raté n'affiche jamais
de résultat partiel, et l'historique reste consultable.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
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


class AnalysisStatus(str, enum.Enum):
    running = "running"
    ok = "ok"
    error = "error"
    # Trop peu de votes pour que PCA + k-means veuille dire quelque chose.
    insufficient_data = "insufficient_data"


class AnalysisRun(Base):
    __tablename__ = "analysis_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[AnalysisStatus] = mapped_column(
        Enum(AnalysisStatus, name="analysis_status"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    n_participants: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_statements: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_votes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    k: Mapped[int | None] = mapped_column(Integer, nullable=True)
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: étiquette k-means -> identité stable, telle que retenue par l'appariement.
    #: Conservée pour pouvoir expliquer après coup pourquoi un groupe a gardé
    #: (ou perdu) son identité.
    group_mapping: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- clivage et consensus du débat (chantier L2) ---------------------------------
    # Ces trois valeurs sont posées ICI et non sur `conversation` : la purge conserve
    # `analysis_run` pour toujours et rend les grosses tables, donc le score d'un débat
    # survit à la disparition du détail qui l'a produit. Sur `conversation`, ce serait
    # une valeur à maintenir à jour — donc une valeur qui finirait par mentir.
    #: Moyenne des `clivage.N_RETENUES` propositions les plus clivantes. NULL tant
    #: qu'aucune proposition n'est notée : « pas encore mesuré » n'est pas « pas
    #: clivant », et les écrans du L4 en dépendent pour ranger.
    clivage: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Moyenne des `clivage.N_RETENUES` propositions les plus consensuelles. Indépendante
    #: de la précédente : un débat peut être à la fois très clivant et très consensuel.
    consensus: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Combien de propositions dépassent `clivage.SEUIL_CLIVANTE`. Le seul des trois qui
    #: s'affichera jamais.
    n_clivantes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    projections: Mapped[list["ParticipantProjection"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    statement_stats: Mapped[list["StatementStat"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<AnalysisRun {self.id} conv={self.conversation_id} {self.status.value}>"


class ParticipantProjection(Base):
    __tablename__ = "participant_projection"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participant.id", ondelete="CASCADE"), nullable=False
    )
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    #: Étiquette brute de k-means : ARBITRAIRE, elle change à chaque recalcul.
    cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Identité stable dans le temps, obtenue en appariant les groupes du run
    #: précédent par recouvrement de composition. C'est la seule qu'on affiche.
    stable_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    run: Mapped["AnalysisRun"] = relationship(back_populates="projections")


class StatementStat(Base):
    __tablename__ = "statement_stat"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("statement.id", ondelete="CASCADE"), nullable=False
    )
    # NULL = statistique globale (toutes tendances confondues).
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Même appariement que pour les participants, pour que les propositions
    #: représentatives d'un groupe restent attachées au bon groupe d'un run à l'autre.
    stable_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    repness: Mapped[float | None] = mapped_column(Float, nullable=True)
    p_test: Mapped[float | None] = mapped_column(Float, nullable=True)
    repful_for: Mapped[str | None] = mapped_column(String(16), nullable=True)
    n_agree: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_disagree: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_seen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    priority: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- clivage et consensus de la proposition (chantier L2) ------------------------
    # Renseignés sur la ligne globale (`group_id NULL`) seulement : ce sont des mesures
    # SUR les groupes, elles n'appartiennent à aucun d'eux.
    #: `1 − consensus`, moyenne géométrique comprise (`services/clivage.py`). NULL quand
    #: la proposition ne mérite pas de score — pas assez vue, un seul groupe, valeur
    #: manquante. **NULL n'est pas zéro** : c'est l'absence de mesure.
    clivage: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Les deux produits bruts de red-dwarf (*group-aware consensus*), tels quels, AVANT
    #: la racine k-ième. Écrits même quand `clivage` est NULL : ce sont les données, pas
    #: le score, et les conserver permettra de rebaisser le plancher ou de déplacer le
    #: seuil sans attendre un nouveau passage d'analyse. Un lecteur qui veut le score
    #: lit `clivage`, jamais ces deux-là.
    consensus_accord: Mapped[float | None] = mapped_column(Float, nullable=True)
    consensus_desaccord: Mapped[float | None] = mapped_column(Float, nullable=True)

    run: Mapped["AnalysisRun"] = relationship(back_populates="statement_stats")


class BaseCluster(Base):
    """Un « seau » de la couche intermédiaire (chantier D, option C').

    Cette table est un **cache**, pas une source de vérité : elle ne fait qu'accélérer
    et stabiliser le calcul suivant. Si elle est perdue ou incohérente, le tour suivant
    repart à froid — exactement comme le moteur de Pol.is après un redémarrage. Rien de
    ce qui est affiché à un participant n'en dépend directement : les noms de groupes
    restent produits par l'appariement de composition de C6.
    """

    __tablename__ = "base_cluster"
    __table_args__ = (
        UniqueConstraint("conversation_id", "bucket_id", name="uq_base_cluster_bucket"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Identifiant du seau DANS la conversation, transmis d'un recalcul à l'autre.
    #: C'est lui qui porte la continuité ; il n'est jamais recyclé.
    bucket_id: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Redondant — le centre se recalcule depuis les membres et la projection du tour
    #: courant — mais conservé pour pouvoir auditer après coup.
    center_x: Mapped[float] = mapped_column(Float, nullable=False)
    center_y: Mapped[float] = mapped_column(Float, nullable=False)

    members: Mapped[list["BaseClusterMember"]] = relationship(
        back_populates="base_cluster", cascade="all, delete-orphan"
    )


class BaseClusterMember(Base):
    __tablename__ = "base_cluster_member"

    id: Mapped[int] = mapped_column(primary_key=True)
    base_cluster_id: Mapped[int] = mapped_column(
        ForeignKey("base_cluster.id", ondelete="CASCADE"), nullable=False, index=True
    )
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participant.id", ondelete="CASCADE"), nullable=False
    )

    base_cluster: Mapped["BaseCluster"] = relationship(back_populates="members")
