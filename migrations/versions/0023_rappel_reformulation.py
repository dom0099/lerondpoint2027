"""Chantier Modération, MOD-15 — le rappel porte une seconde forme.

**Pourquoi une colonne de plus plutôt que celle qui existe.** `statement.rappel_ferme_le`
(migration 0021) ferme le rappel d'un **retrait**. Le rappel ajouté ici naît d'un
**signalement rattrapable** sur une proposition qui, elle, reste en ligne. Les deux
peuvent viser la même proposition : un texte signalé « mal formulé », puis retiré pour un
autre motif. Réemployer la même colonne aurait fait qu'en écartant le premier rappel
l'auteur aurait éteint le second — c'est-à-dire l'exposé des motifs d'un retrait, qui est
**dû** au titre du DSA. Une commodité de schéma aurait supprimé une obligation légale.

**Et pourquoi sur `signalement` plutôt que sur `statement`.** Le rappel existe à cause
d'un signalement ; il doit mourir avec lui et renaître avec le suivant. Posée sur la
proposition, la fermeture vaudrait pour toujours : une proposition reprise, republiée,
puis signalée de nouveau des mois plus tard n'avertirait plus son auteur, parce qu'il
avait écarté un rappel sans rapport. Posée sur le signalement, chaque plainte neuve
reparle.

Additive et nullable : aucune ligne existante n'est touchée, NULL valant « jamais
fermé » — la même convention qu'au MOD-6 pour `rappel_ferme_le` et qu'au MOD-13 pour
`traite_le`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_rappel_reformulation"
down_revision: str | None = "0022_suppression_relecture"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "signalement",
        sa.Column("rappel_ferme_le", sa.DateTime(timezone=True), nullable=True),
    )
    # Aucun index, pour la même raison qu'à la 0021 : la requête du rappel part de
    # `statement.author_participant_id`, puis joint `signalement.statement_id` — tous
    # deux déjà indexés. Le filtre sur cette colonne porte sur la poignée de lignes qui
    # a survécu aux deux premiers. Un index de plus ne ferait que ralentir chaque dépôt
    # de signalement, qui est le chemin qu'il faut garder rapide.


def downgrade() -> None:
    op.drop_column("signalement", "rappel_ferme_le")
