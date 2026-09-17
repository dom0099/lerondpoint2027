"""Chantier J — liens des propositions, chiffres publics et thèmes.

Une seule migration pour les deux blocs du chantier, plutôt que deux à une semaine
d'écart. **Aucun écran ne bouge à cette étape** : elle passe en production sans rien
changer de visible, ce qui la rend facile à vérifier et facile à annuler.

Aucune reprise de données. Les propositions existantes n'ont pas de lien, les débats
existants n'ont ni chiffre ni thème, et les participants n'ont pas de préférence — ce
sont les états de départ voulus. L'étiquetage des débats déjà en ligne est un travail
manuel, à faire **avant** que la règle « au moins un thème » ne soit exigée à la
publication, sans quoi ils deviendraient inéditables.

Rien de ce qui est ajouté ici n'entre dans le calcul des groupes : ces données vivent
à côté du moteur, pas dedans. Un `downgrade` complet ne fait donc perdre que ce que
le chantier J a saisi.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_sources_et_themes"
down_revision: str | None = "0009_couche_intermediaire"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- les liens d'une proposition, deux au plus --------------------------------
    op.create_table(
        "statement_source",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "statement_id",
            sa.Integer(),
            sa.ForeignKey("statement.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=True),
        # Le plafond de deux liens est une règle de la BASE, et non du seul formulaire :
        # le back-office, la console et un import futur ne passent pas par le
        # formulaire, et devraient pourtant s'y plier.
        sa.CheckConstraint("position in (1, 2)", name="ck_statement_source_position"),
        sa.UniqueConstraint("statement_id", "position", name="uq_statement_source_position"),
    )
    op.create_index(
        "ix_statement_source_statement_id", "statement_source", ["statement_id"]
    )

    # --- les chiffres publics d'un débat ------------------------------------------
    op.create_table(
        "conversation_source",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Integer(),
            sa.ForeignKey("conversation.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("titre", sa.String(length=300), nullable=False),
        # Le chiffre en TEXTE : « 12,4 % », « 3 200 communes ». Un numérique
        # obligerait à porter l'unité dans une colonne voisine et à refabriquer la
        # mise en forme à l'affichage, pour une valeur que personne n'additionne.
        sa.Column("valeur", sa.String(length=120), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        # DEUX dates et non une : « mis à jour le 5 septembre » ne dit pas si la
        # donnée est de 2026 ou de 2019, et c'est la confusion qu'une page de chiffres
        # doit empêcher.
        sa.Column("date_donnees", sa.Date(), nullable=True),
        sa.Column("date_verification", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_conversation_source_conversation_id", "conversation_source", ["conversation_id"]
    )

    # --- les thèmes d'un débat ----------------------------------------------------
    # Pas de clé étrangère sur `code` : la liste des thèmes vit comme constante Python
    # (app/services/themes.py), pas comme table. Un code retiré de la constante cesse
    # d'être proposé et affiché ; les lignes qui le portent restent, sans rien casser.
    op.create_table(
        "conversation_theme",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Integer(),
            sa.ForeignKey("conversation.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.UniqueConstraint("conversation_id", "code", name="uq_conversation_theme"),
    )
    op.create_index(
        "ix_conversation_theme_conversation_id", "conversation_theme", ["conversation_id"]
    )
    # Index sur `code` seul : c'est le sens dans lequel le filtre de l'accueil
    # interroge la table — « quels débats portent ce code ? », jamais l'inverse.
    op.create_index("ix_conversation_theme_code", "conversation_theme", ["code"])

    # --- les thèmes de prédilection d'une personne --------------------------------
    # Sur `participant` et non sur `user` : le compte est facultatif sur ce site, donc
    # une préférence portée par le compte ne vaudrait que pour une minorité. Ici, elle
    # vaut aussi pour un visiteur anonyme et survit à la création du compte sans
    # transfert à écrire — c'est la même ligne qui est rattachée.
    # NULL = jamais réglé, ce qui n'est pas « aucun thème ».
    op.add_column("participant", sa.Column("themes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("participant", "themes")
    op.drop_index("ix_conversation_theme_code", table_name="conversation_theme")
    op.drop_index("ix_conversation_theme_conversation_id", table_name="conversation_theme")
    op.drop_table("conversation_theme")
    op.drop_index(
        "ix_conversation_source_conversation_id", table_name="conversation_source"
    )
    op.drop_table("conversation_source")
    op.drop_index("ix_statement_source_statement_id", table_name="statement_source")
    op.drop_table("statement_source")
