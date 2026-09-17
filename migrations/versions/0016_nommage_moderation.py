"""Chantier E5 — la modération des noms générés, avant tout affichage.

Quatre colonnes sur `group_naming`. Le type `moderation_status` existe déjà (il porte la
modération des conversations et des propositions depuis le chantier C) : on le réemploie
plutôt que d'en créer un jumeau, parce que c'est la même décision — un humain accepte ou
refuse une publication — et que deux énumérations parallèles finiraient par diverger.

**`nom` n'est pas touché ; `nom_valide` s'ajoute à côté.** Le modérateur peut corriger
avant de valider, mais ce que le modèle a écrit doit rester lisible : c'est ce qui permet
à la mention publique du E6 de distinguer « validé » de « corrigé et validé », et c'est
la seule trace exploitable pour mesurer la qualité réelle du modèle.

Aucune reprise de données : les quatre noms déjà produits passent en `pending` par le
défaut de colonne, donc ils cessent d'être affichables — ce qu'ils n'étaient pas encore,
aucun gabarit ne lisant cette table. La migration ne change donc rien de visible.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_nommage_moderation"
down_revision: str | None = "0015_nommage_file"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `create_type=False` : le type existe depuis le chantier C. Le recréer ferait échouer
#: la migration sur « type already exists » — c'est le motif des migrations 0003 et 0004,
#: pris à l'envers.
STATUT = sa.Enum(
    "pending", "approved", "rejected", name="moderation_status", create_type=False
)


def upgrade() -> None:
    op.add_column("group_naming", sa.Column("nom_valide", sa.String(120), nullable=True))
    op.add_column(
        "group_naming",
        sa.Column("statut", STATUT, nullable=False, server_default="pending"),
    )
    op.add_column(
        "group_naming",
        sa.Column(
            "modere_par_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "group_naming", sa.Column("modere_le", sa.DateTime(timezone=True), nullable=True)
    )
    # L'index de la lecture publique : « le dernier nom VALIDÉ de chaque groupe de ce
    # débat ». Partiel, comme celui de la file : les lignes en attente ou refusées ne
    # sont jamais lues par ce chemin, et les indexer ferait grossir l'index avec ce
    # qu'on ne cherche pas.
    op.create_index(
        "ix_group_naming_valides",
        "group_naming",
        ["conversation_id", "stable_group_id", "decided_at"],
        postgresql_where=sa.text("statut = 'approved'"),
    )


def downgrade() -> None:
    op.drop_index("ix_group_naming_valides", table_name="group_naming")
    op.drop_column("group_naming", "modere_le")
    op.drop_column("group_naming", "modere_par_id")
    op.drop_column("group_naming", "statut")
    op.drop_column("group_naming", "nom_valide")
    # Le type `moderation_status` n'est PAS supprimé : il servait avant cette migration
    # et sert encore après. Le retirer casserait la modération des conversations.
