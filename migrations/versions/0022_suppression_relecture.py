"""Chantier Modération, MOD-14 — la table `relecture` est supprimée.

**Ce que ce lot défait, et pourquoi.** La grille de relecture (MOD-4) s'appliquait
AVANT publication. dom a décidé le 15 septembre 2026 qu'il n'y aurait plus de relecture
avant publication : une proposition déposée est publiée, et le contrôle se fait après,
par le signalement. La grille n'a donc plus de moment où s'appliquer, et la table plus
rien à recevoir.

**Sans perte de données, vérifié et non supposé.** Le relevé en lecture seule fait sur
la base de production avant d'écrire ce lot rendait **0 relecture** : la grille a été
livrée, déployée, et n'a jamais servi une seule fois. La consigne exigeait cette
vérification avant toute suppression, et elle conditionnait le choix de l'option — sur
un corpus non vide, on aurait gardé la table.

**Le `downgrade` recrée la table, vide.** Il ne peut pas faire mieux : une descente ne
ressuscite pas des lignes qu'une montée a supprimées. Elle est écrite à l'identique de
la 0020 — mêmes colonnes, même contrainte, mêmes index — pour qu'un aller-retour rende
exactement le schéma d'avant.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_suppression_relecture"
down_revision: str | None = "0021_rappel_auteur"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Les quatre index et la contrainte partent avec la table : PostgreSQL les supprime
    # en même temps qu'elle, il n'y a rien à défaire séparément.
    op.drop_table("relecture")


def downgrade() -> None:
    op.create_table(
        "relecture",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cible_type", sa.String(32), nullable=False),
        sa.Column("cible_id", sa.Integer(), nullable=False),
        sa.Column("origine", sa.String(32), nullable=False),
        sa.Column("une_seule_idee", sa.String(16), nullable=False),
        sa.Column("comprehensible_seule", sa.String(16), nullable=False),
        sa.Column("deja_proposee_id", sa.Integer(), nullable=True),
        sa.Column("nature", sa.String(16), nullable=False),
        sa.Column("sourcee", sa.String(16), nullable=False),
        sa.Column("vise", sa.String(16), nullable=False),
        sa.Column("issue_derivee", sa.String(16), nullable=False),
        sa.Column("issue_retenue", sa.String(16), nullable=False),
        sa.Column("motif_derogation", sa.String(500), nullable=True),
        sa.Column("expose", sa.Text(), nullable=False),
        sa.Column("auteur", sa.String(160), nullable=False),
        sa.Column("duree_secondes", sa.Integer(), nullable=True),
        sa.Column(
            "cree_le",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "issue_retenue = issue_derivee OR "
            "(motif_derogation IS NOT NULL AND length(btrim(motif_derogation)) > 0)",
            name="ck_relecture_derogation_motivee",
        ),
    )
    op.create_index("ix_relecture_cible", "relecture", ["cible_type", "cible_id"])
    op.create_index("ix_relecture_issue_derivee", "relecture", ["issue_derivee"])
    op.create_index("ix_relecture_issue_retenue", "relecture", ["issue_retenue"])
    op.create_index("ix_relecture_cree_le", "relecture", ["cree_le"])
