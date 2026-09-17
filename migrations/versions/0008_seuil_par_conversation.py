"""Seuil de participation réglable par conversation.

`min_user_votes` NULL — le cas de toutes les conversations existantes — signifie
« borné automatiquement par le nombre de déclarations approuvées ». La valeur fixe de
7 héritée de red-dwarf rendait l'analyse *mathématiquement impossible* sur une
conversation de moins de 7 déclarations : aucun participant ne pouvait atteindre le
seuil, quel que soit leur nombre. Aucune reprise de données n'est nécessaire, la
borne s'applique au prochain recalcul.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_seuil_par_conversation"
down_revision: str | None = "0007_groupes_stables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversation", sa.Column("min_user_votes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversation", "min_user_votes")
