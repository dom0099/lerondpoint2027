"""C6 — identité stable des groupes d'opinion.

k-means numérote ses groupes arbitrairement à chaque exécution. `stable_group_id`
porte l'identité conservée d'un calcul à l'autre, obtenue par appariement sur le
recouvrement de composition ; `cluster_id` reste l'étiquette brute, gardée pour
pouvoir expliquer un appariement après coup.

Les runs antérieurs à cette migration n'ont pas d'identité stable : la colonne reste
NULL pour eux, et le premier recalcul qui suit fonde les identités.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_groupes_stables"
down_revision: str | None = "0006_limites"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "participant_projection",
        sa.Column("stable_group_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "statement_stat", sa.Column("stable_group_id", sa.Integer(), nullable=True)
    )
    op.add_column("analysis_run", sa.Column("group_mapping", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_run", "group_mapping")
    op.drop_column("statement_stat", "stable_group_id")
    op.drop_column("participant_projection", "stable_group_id")
