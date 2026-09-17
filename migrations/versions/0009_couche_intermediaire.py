"""Couche intermédiaire de seaux et lisseur du nombre de groupes (chantier D, C').

Les groupes d'opinion sont désormais calculés sur des « seaux » pondérés plutôt que sur
les participants bruts, et cette couche est réamorcée d'un recalcul à l'autre. Ce qui
est ajouté ici est un **cache** : `base_cluster`, `base_cluster_member` et les colonnes
de `conversation` peuvent être vidés sans perte de donnée — le calcul suivant repartira
simplement à froid. Aucune reprise de données n'est nécessaire : les conversations
existantes partiront à froid à leur prochain recalcul, ce qui est le comportement voulu.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_couche_intermediaire"
down_revision: str | None = "0008_seuil_par_conversation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "base_cluster",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Integer(),
            sa.ForeignKey("conversation.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("bucket_id", sa.Integer(), nullable=False),
        sa.Column("center_x", sa.Float(), nullable=False),
        sa.Column("center_y", sa.Float(), nullable=False),
        sa.UniqueConstraint("conversation_id", "bucket_id", name="uq_base_cluster_bucket"),
    )
    op.create_index("ix_base_cluster_conversation_id", "base_cluster", ["conversation_id"])

    op.create_table(
        "base_cluster_member",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "base_cluster_id",
            sa.Integer(),
            sa.ForeignKey("base_cluster.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "participant_id",
            sa.Integer(),
            sa.ForeignKey("participant.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_base_cluster_member_base_cluster_id", "base_cluster_member", ["base_cluster_id"]
    )

    op.add_column("conversation", sa.Column("group_partition", sa.JSON(), nullable=True))
    op.add_column("conversation", sa.Column("smoother_last_k", sa.Integer(), nullable=True))
    op.add_column(
        "conversation", sa.Column("smoother_last_k_count", sa.Integer(), nullable=True)
    )
    op.add_column("conversation", sa.Column("smoother_smoothed_k", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversation", "smoother_smoothed_k")
    op.drop_column("conversation", "smoother_last_k_count")
    op.drop_column("conversation", "smoother_last_k")
    op.drop_column("conversation", "group_partition")
    op.drop_index("ix_base_cluster_member_base_cluster_id", table_name="base_cluster_member")
    op.drop_table("base_cluster_member")
    op.drop_index("ix_base_cluster_conversation_id", table_name="base_cluster")
    op.drop_table("base_cluster")
