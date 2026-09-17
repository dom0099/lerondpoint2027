"""Chantier Modération, MOD-6 (lot de repli) — le rappel à l'auteur.

**Une colonne nullable, et rien d'autre.** La migration la plus petite du chantier.

`statement.rappel_ferme_le` retient que l'auteur a fermé le rappel concernant CETTE
proposition. Une colonne sur `statement` plutôt qu'une table `rappel_ferme` : il y a au
plus un rappel par proposition, et son destinataire est déjà désigné par
`author_participant_id`. Une table d'association n'aurait porté aucune information que la
proposition ne porte pas, et aurait demandé une jointure à chaque affichage d'une page
publique très fréquentée.

NULL = jamais fermé, donc le rappel est dû. C'est le défaut, et c'est le bon : une
proposition retirée avant ce lot sort de la migration avec un rappel à montrer, ce qui est
exactement ce qu'on veut — son auteur n'a jamais été prévenu.

**Le rappel ne réapparaît pas après fermeture**, mais il réapparaîtra si une AUTRE
proposition du même auteur est retirée : la colonne est par proposition, pas par
personne. C'est voulu — chaque retrait est une décision distincte, et son exposé des
motifs est dû pour lui-même.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_rappel_auteur"
down_revision: str | None = "0020_relecture"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "statement",
        sa.Column("rappel_ferme_le", sa.DateTime(timezone=True), nullable=True),
    )
    # Aucun index : la requête du rappel part de `author_participant_id`, déjà indexé par
    # sa clé étrangère, et filtre ensuite sur deux colonnes d'une poignée de lignes. Un
    # index ici ne servirait qu'à ralentir chaque écriture de proposition.


def downgrade() -> None:
    op.drop_column("statement", "rappel_ferme_le")
