"""Chantier L2 — le clivage et le consensus, enfin écrits.

Six colonnes, toutes `nullable`, aucune table nouvelle et **aucune reprise de données**.
Les valeurs naîtront au calcul suivant, au plus tard un quart d'heure après la mise en
ligne (`analysis_interval_seconds = 900`) : rien n'est réécrit rétroactivement, et rien
n'a besoin de l'être.

**Pourquoi le score du débat va sur `analysis_run` et non sur `conversation`.** La purge
(`purge_calculs_perimes`) rend `statement_stat` et `participant_projection` des calculs
dépassés, mais conserve `analysis_run` pour toujours. Le score d'un débat survit donc à
la purge du détail qui l'a produit, ce qui est exactement la bonne durée de vie pour
chacun des deux. Sur `conversation`, ce serait une valeur à maintenir à jour — donc une
valeur qui finirait par mentir.

**NULL n'est pas zéro.** Aucune de ces colonnes n'a de `server_default` : une
proposition trop peu vue, un débat sans calcul abouti, un débat à un seul groupe n'ont
pas un clivage nul, ils n'ont pas de clivage. Un défaut à 0 aurait déclaré consensuels
tous les débats que personne n'a encore mesurés.

Le `downgrade` ne fait perdre que ce que le L a mesuré : le reste de l'analyse ignore
ces colonnes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_clivage_et_consensus"
down_revision: str | None = "0011_liens_verifies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Par proposition, sur la ligne globale (`group_id NULL`) : le score, et les deux
    # produits bruts de red-dwarf dont il est tiré. Garder les bruts est ce qui
    # permettra de déplacer le seuil ou le plancher sans attendre un nouveau passage.
    op.add_column("statement_stat", sa.Column("clivage", sa.Float(), nullable=True))
    op.add_column(
        "statement_stat", sa.Column("consensus_accord", sa.Float(), nullable=True)
    )
    op.add_column(
        "statement_stat", sa.Column("consensus_desaccord", sa.Float(), nullable=True)
    )

    # Par calcul : les deux moyennes du débat — indépendantes l'une de l'autre — et le
    # seul entier qui s'affichera.
    op.add_column("analysis_run", sa.Column("clivage", sa.Float(), nullable=True))
    op.add_column("analysis_run", sa.Column("consensus", sa.Float(), nullable=True))
    op.add_column("analysis_run", sa.Column("n_clivantes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_run", "n_clivantes")
    op.drop_column("analysis_run", "consensus")
    op.drop_column("analysis_run", "clivage")
    op.drop_column("statement_stat", "consensus_desaccord")
    op.drop_column("statement_stat", "consensus_accord")
    op.drop_column("statement_stat", "clivage")
