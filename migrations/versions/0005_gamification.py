"""C5 — niveaux/badges et conversations proposées par les participants.

Note de convention : `alembic_version.version_num` est un varchar(32).
Un identifiant de révision plus long fait échouer la migration à l'écriture,
après l'exécution du DDL — d'où un message trompeur. Les identifiants
doivent rester courts.

Le type ENUM `moderation_status` existe déjà (créé en 0003) : il est réutilisé tel
quel avec `create_type=False`, sans quoi PostgreSQL refuserait un type homonyme.

Les conversations existantes sont marquées `approved` : elles ont toutes été créées
par un modérateur, donc déjà validées de fait.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_gamification"
down_revision: str | None = "0004_votes_et_analyse"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MODERATION_STATUS = postgresql.ENUM(
    "pending", "approved", "rejected", name="moderation_status", create_type=False
)

BADGES = [
    {
        "code": "first_vote",
        "label": "Premier vote",
        "description": "Décerné au premier vote émis.",
        "icon": "🗳️",
    },
    {
        "code": "first_conversation",
        "label": "Première conversation créée",
        "description": "Décerné quand une conversation que vous avez proposée est publiée.",
        "icon": "💬",
    },
]


def upgrade() -> None:
    # --- conversations proposées par les participants -----------------------------
    op.add_column(
        "conversation",
        sa.Column(
            "moderation_status",
            MODERATION_STATUS,
            nullable=False,
            server_default="approved",
        ),
    )
    op.create_index(
        op.f("ix_conversation_moderation_status"),
        "conversation",
        ["moderation_status"],
    )
    op.add_column(
        "conversation",
        sa.Column("proposed_by_participant_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_conversation_proposed_by",
        "conversation",
        "participant",
        ["proposed_by_participant_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- badges -------------------------------------------------------------------
    badge = op.create_table(
        "badge",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("icon", sa.String(length=8), nullable=False),
        sa.PrimaryKeyConstraint("code"),
    )
    op.bulk_insert(badge, BADGES)

    op.create_table(
        "user_badge",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("badge_code", sa.String(length=64), nullable=False),
        sa.Column(
            "awarded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["badge_code"], ["badge.code"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Porte la règle « un badge une seule fois » : deux votes simultanés ne
        # peuvent pas décerner deux fois « Premier vote ».
        sa.UniqueConstraint("user_id", "badge_code", name="uq_user_badge"),
    )
    op.create_index(op.f("ix_user_badge_user_id"), "user_badge", ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_user_badge_user_id"), table_name="user_badge")
    op.drop_table("user_badge")
    op.drop_table("badge")
    op.drop_constraint("fk_conversation_proposed_by", "conversation", type_="foreignkey")
    op.drop_column("conversation", "proposed_by_participant_id")
    op.drop_index(op.f("ix_conversation_moderation_status"), table_name="conversation")
    op.drop_column("conversation", "moderation_status")
    # Le type ENUM n'est PAS supprimé : la table `statement` l'utilise toujours.
