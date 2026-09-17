"""C2 — conversations et déclarations.

Deux corrections par rapport à l'autogénération d'Alembic :
  - l'import de `fastapi_users_db_sqlalchemy` (le type GUID est référencé mais
    l'import n'est pas émis) ;
  - la suppression explicite des types ENUM PostgreSQL au `downgrade`. Sans elle,
    les types survivent à la suppression des tables et un nouvel `upgrade` échoue
    sur « type already exists ».
"""

from collections.abc import Sequence

import fastapi_users_db_sqlalchemy
import sqlalchemy as sa
from alembic import op

revision: str = "0003_conversations_declarations"
down_revision: str | None = "0002_comptes_participants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONVERSATION_STATE = sa.Enum("draft", "open", "closed", name="conversation_state")
MODERATION_MODE = sa.Enum("pre", "post", name="moderation_mode")
MODERATION_STATUS = sa.Enum("pending", "approved", "rejected", name="moderation_status")


def upgrade() -> None:
    op.create_table(
        "conversation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "owner_user_id",
            fastapi_users_db_sqlalchemy.generics.GUID(),
            nullable=True,
        ),
        sa.Column("state", CONVERSATION_STATE, nullable=False),
        sa.Column("is_public", sa.Boolean(), nullable=False),
        sa.Column("allow_participant_statements", sa.Boolean(), nullable=False),
        sa.Column("moderation_mode", MODERATION_MODE, nullable=False),
        sa.Column("force_group_count", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_conversation_slug"), "conversation", ["slug"], unique=True
    )

    op.create_table(
        "statement",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("author_participant_id", sa.Integer(), nullable=True),
        sa.Column("is_seed", sa.Boolean(), nullable=False),
        sa.Column("is_meta", sa.Boolean(), nullable=False),
        sa.Column("moderation_status", MODERATION_STATUS, nullable=False),
        sa.Column(
            "moderated_by_user_id",
            fastapi_users_db_sqlalchemy.generics.GUID(),
            nullable=True,
        ),
        sa.Column("moderated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["author_participant_id"], ["participant.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversation.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["moderated_by_user_id"], ["user.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_statement_conversation_id"), "statement", ["conversation_id"]
    )
    op.create_index(
        op.f("ix_statement_moderation_status"), "statement", ["moderation_status"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_statement_moderation_status"), table_name="statement")
    op.drop_index(op.f("ix_statement_conversation_id"), table_name="statement")
    op.drop_table("statement")
    op.drop_index(op.f("ix_conversation_slug"), table_name="conversation")
    op.drop_table("conversation")

    bind = op.get_bind()
    MODERATION_STATUS.drop(bind, checkfirst=True)
    MODERATION_MODE.drop(bind, checkfirst=True)
    CONVERSATION_STATE.drop(bind, checkfirst=True)
