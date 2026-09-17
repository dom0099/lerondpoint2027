"""Chantier E1 — le journal des décisions de nommage des groupes.

Une table, aucune colonne ajoutée ailleurs, aucune reprise de données : les lignes
naîtront au premier passage du worker qui suivra. La migration passe donc en production
sans rien changer de visible, et son `downgrade` ne fait perdre que ce que le E1 a
mesuré.

**Rien ne nomme encore.** Le E1 tourne sans LLM : chaque ligne dit « ce groupe aurait
été nommé maintenant, à cause de ceci ». `nom` et `justification` sont créés nuls dès
maintenant pour que le E4 les remplisse sans seconde migration — c'est la même ligne qui
portera la décision et son résultat.

**Pas de colonne « première apparition », et c'est un choix.** La règle du §5 exige
qu'une identité de groupe tienne depuis trois heures avant d'être nommée. Cette
ancienneté ne se stocke pas ici : elle se relit dans `analysis_run.group_mapping`, que
le G9 a explicitement exclue de la purge (« jamais effacée : sa colonne `group_mapping`
est l'historique »). Dupliquer une donnée déjà conservée aurait créé deux vérités à
tenir d'accord, et la copie aurait dérivé la première fois qu'un calcul échoue.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_nommage_des_groupes"
down_revision: str | None = "0012_clivage_et_consensus"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MOTIF = sa.Enum("nouveau", "changement", name="motif_nommage")


def upgrade() -> None:
    # Pas de `MOTIF.create()` explicite : `create_table` crée le type avec la colonne
    # qui l'emploie. Même motif qu'aux migrations 0003, 0004 et 0011 — on ne crée
    # jamais le type à la main, on le supprime toujours à la main.
    op.create_table(
        "group_naming",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Integer(),
            sa.ForeignKey("conversation.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stable_group_id", sa.Integer(), nullable=False),
        # `SET NULL` et non `CASCADE` : la purge du G9 ne touche pas `analysis_run`,
        # mais une conversation rejouée à la main pourrait en supprimer un. Perdre le
        # lien vers le calcul est acceptable ; perdre la décision fausserait la mesure,
        # qui est tout l'objet de cette table.
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("analysis_run.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("motif", MOTIF, nullable=False),
        # Des COUPLES `[déclaration, sens]`, pas des identifiants nus : deux groupes
        # opposés partagent l'essentiel de leurs déclarations représentatives et ne se
        # distinguent que par le sens (mesuré sur le débat du permis à 16 ans, calcul
        # 27). Sans le sens, un groupe qui retourne complètement sa position présente
        # un ensemble inchangé et ne déclencherait aucun renommage.
        sa.Column(
            "declarations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("nom", sa.String(length=120), nullable=True),
        sa.Column("justification", sa.Text(), nullable=True),
    )
    # L'index de la seule requête chaude : « la dernière décision de ce groupe », posée
    # une fois par groupe et par calcul. `decided_at` décroissant y est inclus pour que
    # le `LIMIT 1` se serve dans l'index au lieu de trier les décisions d'un groupe âgé.
    op.create_index(
        "ix_group_naming_derniere",
        "group_naming",
        ["conversation_id", "stable_group_id", sa.text("decided_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_group_naming_derniere", table_name="group_naming")
    op.drop_table("group_naming")
    # Le type, lui, se supprime à la main : `drop_table` ne l'emporte pas.
    MOTIF.drop(op.get_bind(), checkfirst=True)
