"""Racine de la chaîne de migrations.

Volontairement vide : l'étape C0 ne crée aucune table. Cette révision existe pour
que la chaîne ait une racine stable et que `alembic upgrade head` crée bien la
table `alembic_version`. Les tables arrivent à C1 (comptes) et C2 (conversations).
"""

from collections.abc import Sequence

revision: str = "0001_racine"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
