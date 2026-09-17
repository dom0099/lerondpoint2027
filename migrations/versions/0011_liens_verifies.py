"""Chantier K — l'état de vérification des adresses publiées.

Une table, aucune colonne ajoutée ailleurs, aucune reprise de données : les lignes
naîtront au premier passage du worker (K2). La migration passe donc en production sans
rien changer de visible, et son `downgrade` ne fait perdre que ce que le K a mesuré.

**L'unicité porte sur une empreinte, pas sur l'adresse.** Un index B-tree PostgreSQL
refuse une entrée de plus de ~2 700 octets ; `url` fait 2 000 caractères, soit jusqu'à
8 000 octets en UTF-8. Une contrainte posée sur `url` aurait tenu en recette et cédé le
jour où quelqu'un colle une adresse à rallonge — c'est-à-dire au pire moment.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_liens_verifies"
down_revision: str | None = "0010_sources_et_themes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VERDICT = sa.Enum(
    "non_verifie",
    "joignable",
    "injoignable",
    "non_concluant",
    "hors_perimetre",
    name="verdict_lien",
)


def upgrade() -> None:
    # Pas de `VERDICT.create()` explicite : `create_table` crée le type avec la colonne
    # qui l'emploie, et le créer d'abord le faisait exister deux fois — « type
    # verdict_lien already exists ». C'est le motif des migrations 0003 et 0004 : on ne
    # crée jamais le type à la main, on le supprime toujours à la main.
    op.create_table(
        "lien_verifie",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("empreinte", sa.String(length=64), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("domaine", sa.String(length=255), nullable=False),
        sa.Column("verdict", VERDICT, nullable=False, server_default="non_verifie"),
        sa.Column("code_http", sa.Integer(), nullable=True),
        sa.Column("dernier_essai", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dernier_succes", sa.DateTime(timezone=True), nullable=True),
        # Début de la série d'échecs en cours : c'est cette date, et non le dernier
        # essai, qui dit depuis quand une page est introuvable.
        sa.Column("premier_echec", sa.DateTime(timezone=True), nullable=True),
        sa.Column("echecs_consecutifs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("empreinte", name="uq_lien_verifie_empreinte"),
    )
    op.create_index("ix_lien_verifie_domaine", "lien_verifie", ["domaine"])
    op.create_index("ix_lien_verifie_verdict", "lien_verifie", ["verdict"])
    # La sélection du K2 demande « les moins récemment vues » : cet index est celui de
    # la requête du worker, pas une précaution générale.
    op.create_index("ix_lien_verifie_dernier_essai", "lien_verifie", ["dernier_essai"])


def downgrade() -> None:
    op.drop_index("ix_lien_verifie_dernier_essai", table_name="lien_verifie")
    op.drop_index("ix_lien_verifie_verdict", table_name="lien_verifie")
    op.drop_index("ix_lien_verifie_domaine", table_name="lien_verifie")
    op.drop_table("lien_verifie")
    # Le type ENUM survit à la table qui l'employait : sans ce retrait, le second
    # `upgrade` échouerait. C'est exactement ce que le test d'aller-retour des migrations
    # existe pour attraper — et il l'a attrapé, sur la faute inverse : un `create`
    # explicite en trop à l'aller.
    VERDICT.drop(op.get_bind(), checkfirst=True)
