"""Chantier E4 — de quoi vider la file de nommage, et la mesurer.

Trois colonnes sur `group_naming`, aucune table nouvelle : la table du E1 **est** la
file, et `nom IS NULL` en est l'état « en attente ». La décision de nommer et la demande
de nom sont le même fait ; les séparer aurait créé deux vérités à tenir d'accord.

`named_at` n'est pas une commodité de journalisation : c'est la mesure sur laquelle le
E3 a écrit son seuil de réexamen de l'architecture (deux heures de médiane entre
l'empilement et la production). Sans elle, ce seuil serait un vœu.

La migration passe en production sans rien changer de visible : le nommage par modèle est
désactivé par défaut (`naming_enabled`), donc les colonnes restent vides jusqu'à ce qu'un
serveur d'inférence soit délibérément mis en marche.

Numérotée 0015 et non 0014 : le chantier profil avait déjà posé un `0014_bio_du_profil`
sur le même parent, ce qui donnait deux têtes et rendait `alembic upgrade head`
impossible. Deux chantiers menés en parallèle sur la même branche produisent
naturellement ce conflit ; il se résout en chaînant, pas en renumérotant l'autre.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_nommage_file"
down_revision: str | None = "0014_bio_du_profil"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "group_naming", sa.Column("named_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "group_naming",
        sa.Column("tentatives", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("group_naming", sa.Column("erreur", sa.Text(), nullable=True))
    # L'index de la requête du consommateur : « les demandes en attente, les plus
    # anciennes d'abord ». Partiel, parce qu'une file bien tenue est presque toujours
    # vide : indexer les lignes déjà nommées ferait grossir l'index avec l'historique,
    # qui est justement ce qu'on ne consulte jamais par ce chemin.
    op.create_index(
        "ix_group_naming_en_attente",
        "group_naming",
        ["decided_at"],
        postgresql_where=sa.text("nom IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_group_naming_en_attente", table_name="group_naming")
    op.drop_column("group_naming", "erreur")
    op.drop_column("group_naming", "tentatives")
    op.drop_column("group_naming", "named_at")
