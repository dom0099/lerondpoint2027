"""Corrections de la revue des situations limites.

Deux ajouts :
  - `rate_limit_hit`, journal des actions plafonnées (réinitialisation de mot de
    passe pour l'instant) ;
  - unicité `(conversation_id, text)` sur `statement`, pour qu'un double clic ne
    crée plus plusieurs fois la même proposition.

La contrainte ne peut pas être posée si des doublons existent déjà : ils sont donc
supprimés au préalable, en gardant le plus ancien de chaque groupe. Les votes portés
par les copies supprimées partent en cascade — c'est voulu, ce sont des votes sur une
déclaration qui n'aurait jamais dû exister en double.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_limites"
down_revision: str | None = "0005_gamification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_hit",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bucket", sa.String(length=160), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rate_limit_hit_bucket_created",
        "rate_limit_hit",
        ["bucket", "created_at"],
    )

    op.execute(
        """
        DELETE FROM statement s
        USING statement plus_ancienne
        WHERE s.conversation_id = plus_ancienne.conversation_id
          AND s.text = plus_ancienne.text
          AND s.id > plus_ancienne.id
        """
    )
    op.create_unique_constraint(
        "uq_statement_text", "statement", ["conversation_id", "text"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_statement_text", "statement", type_="unique")
    op.drop_index("ix_rate_limit_hit_bucket_created", table_name="rate_limit_hit")
    op.drop_table("rate_limit_hit")
