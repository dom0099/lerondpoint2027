"""Chantier Modération, MOD-4 — la grille de relecture.

**Additive et réversible** : une table créée, rien d'autre. Aucune colonne existante
touchée, aucune énumération élargie, aucune donnée réécrite. C'est la migration la plus
simple du chantier, et le `downgrade` tient en une ligne.

**Pourquoi cette migration suit la 0019 et non la 0018.** Les deux lots MOD-3b et MOD-4
ont été demandés « depuis la même base ». Alembic n'a qu'une histoire linéaire : deux
migrations qui partageraient le parent `0018_signalement` produiraient **deux têtes**, et
`alembic upgrade head` refuserait de choisir (« Multiple head revisions are present »).
La branche `chantier-moderation-4` est donc partie de `chantier-moderation-3b`, et cette
migration se pose derrière la 0019. Conséquence à connaître : **le MOD-3b doit être
fusionné avant le MOD-4**, ce qui est de toute façon l'ordre des deux lots.

Tout ce qui fait la valeur de la table est dans son modèle
(`app/models/moderation.py::Relecture`) : pourquoi les six réponses sont conservées et
pas seulement leur résultat, pourquoi `duree_secondes` est captée alors qu'elle ne sert à
rien aujourd'hui, et pourquoi les deux issues cohabitent.

La seule chose qui mérite d'être répétée ici est la contrainte : **une dérogation sans
motif écrit est refusée par la base**, pas seulement par le service. Le taux de
dérogation est l'indicateur principal du lot ; une dérogation sans motif est exactement
ce qu'on ne pourra plus reconstituer après coup.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_relecture"
down_revision: str | None = "0019_contestation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "relecture",
        sa.Column("id", sa.Integer(), primary_key=True),
        # Aucune clé étrangère vers `statement`, comme pour `journal_moderation` : une
        # relecture décrit un geste, et le geste a eu lieu même si la cible disparaît. Un
        # CASCADE effacerait le corpus en même temps que les propositions, c'est-à-dire
        # précisément ce que les lots participatifs auront à mesurer.
        sa.Column("cible_type", sa.String(32), nullable=False),
        sa.Column("cible_id", sa.Integer(), nullable=False),
        sa.Column("origine", sa.String(32), nullable=False),
        # Les six réponses, en texte : la liste fermée vit dans `app/services/grille.py`,
        # comme les thèmes et les motifs de signalement.
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


def downgrade() -> None:
    # Rien d'autre à défaire : la table n'a modifié aucun type, aucune colonne existante
    # et aucune donnée. Descendre ce lot fait perdre le corpus de relectures — ce qui est
    # le propre d'une table de données, et non un effet de bord à corriger.
    op.drop_table("relecture")
