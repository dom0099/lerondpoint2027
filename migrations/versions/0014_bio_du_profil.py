"""Ajoute la bio du profil.

Une colonne, sur le même schéma que `display_name` (0002) : facultative, aucune
reprise de données. L'avatar n'a pas de colonne — il se calcule à chaque affichage
à partir de `user.id`, déjà présent — et le déplafonnement des centres d'intérêt ne
touche à aucune contrainte de base (`participant.themes` est un `JSON` sans
contrainte de longueur, voir `app/services/themes.py`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_bio_du_profil"
down_revision: str | None = "0013_nommage_des_groupes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user", sa.Column("bio", sa.String(length=280), nullable=True))


def downgrade() -> None:
    op.drop_column("user", "bio")
