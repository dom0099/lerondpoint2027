"""C1 — comptes (fastapi-users) et participants.

`participant.user_id` est UNIQUE et NULLABLE : au plus un participant par compte,
et un participant peut exister sans compte (vote anonyme). C'est ce qui permet à un
anonyme de créer un compte plus tard sans perdre son historique.

Note : Alembic génère le type `GUID` de fastapi-users sans émettre son import ;
celui-ci est ajouté à la main ci-dessous.
"""

from collections.abc import Sequence

import fastapi_users_db_sqlalchemy
import sqlalchemy as sa
from alembic import op

revision: str = "0002_comptes_participants"
down_revision: str | None = "0001_racine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user",
        sa.Column("id", fastapi_users_db_sqlalchemy.generics.GUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("hashed_password", sa.String(length=1024), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_superuser", sa.Boolean(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_user_email"), "user", ["email"], unique=True)

    op.create_table(
        "participant",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "user_id", fastapi_users_db_sqlalchemy.generics.GUID(), nullable=True
        ),
        sa.Column("anon_token", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_index(
        op.f("ix_participant_anon_token"), "participant", ["anon_token"], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_participant_anon_token"), table_name="participant")
    op.drop_table("participant")
    op.drop_index(op.f("ix_user_email"), table_name="user")
    op.drop_table("user")
