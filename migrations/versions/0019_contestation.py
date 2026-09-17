"""Chantier Modération, MOD-3b — la voie de recours.

**Additive et réversible**, comme la 0018 : une table créée, une colonne nullable
ajoutée, une valeur ajoutée à une énumération. Aucune colonne supprimée, aucune donnée
réécrite, aucun renommage.

Ce qu'elle apporte :

  - `statement.jeton_contestation` — le jeton non devinable posé au moment du retrait.
    **C'est le seul droit d'accès à `/contester/<jeton>`**, et il ne peut pas en être
    autrement : l'auteur d'une proposition est anonyme dans 98 % des cas (164
    participants sur 167 au 15 septembre 2026), il n'existe donc aucun compte sur lequel
    adosser une autorisation. Unique, indexé, jamais réémis ;
  - `contestation` — la demande de réexamen elle-même ;
  - `contestation_deposee` dans `acte_moderation`, pour que la demande laisse une trace
    dans le journal d'audit, qui est en ajout seul.

**Rien n'est à reprendre sur l'existant.** Les propositions déjà retirées par la 0018 —
il n'y en a aucune en production, la 0018 n'y étant pas appliquée — sortiraient de cette
migration sans jeton, donc sans recours ouvrable. Le cas est traité dans le code plutôt
qu'ici : `_retirer` pose le jeton s'il manque, et une proposition retirée avant ce lot
en recevrait un au prochain retrait. Une reprise de données dans une migration additive
aurait été un précédent qu'on ne veut pas ouvrir.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_contestation"
down_revision: str | None = "0018_signalement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Comme pour `retire` au 0018 : depuis PostgreSQL 12 cet ordre s'exécute dans une
    # transaction, la seule règle étant de ne pas UTILISER la valeur neuve avant la fin
    # de celle-ci. Cette migration ne l'utilise pas.
    op.execute(
        "ALTER TYPE acte_moderation ADD VALUE IF NOT EXISTS 'contestation_deposee'"
    )

    op.add_column(
        "statement", sa.Column("jeton_contestation", sa.String(64), nullable=True)
    )
    # UNIQUE et non un simple index : deux propositions qui partageraient un jeton
    # ouvriraient chacune la porte de l'autre. La contrainte le rend impossible même si
    # le tirage aléatoire venait à se répéter — ce qui n'arrivera pas sur 256 bits, mais
    # une garantie qui repose sur une probabilité n'est pas une garantie.
    op.create_index(
        "ix_statement_jeton_contestation",
        "statement",
        ["jeton_contestation"],
        unique=True,
    )

    op.create_table(
        "contestation",
        sa.Column("id", sa.Integer(), primary_key=True),
        # CASCADE : une contestation n'a aucun sens sans la proposition qu'elle défend.
        # Ce qui doit survivre à la suppression est dans `journal_moderation`, qui n'a
        # volontairement aucune clé étrangère.
        sa.Column(
            "statement_id",
            sa.Integer(),
            sa.ForeignKey("statement.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("texte", sa.String(1000), nullable=False),
        sa.Column(
            "cree_le",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("traite_le", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_contestation_statement_id", "contestation", ["statement_id"])
    op.create_index("ix_contestation_cree_le", "contestation", ["cree_le"])


def downgrade() -> None:
    op.drop_table("contestation")
    op.drop_index("ix_statement_jeton_contestation", table_name="statement")
    op.drop_column("statement", "jeton_contestation")

    # Retirer une valeur d'une énumération demande de recréer le type — même manœuvre
    # qu'au 0018 pour `retire`, en plus simple : `acte_moderation` ne porte qu'une seule
    # colonne, sans valeur par défaut et sans index partiel qui le cite.
    #
    # Les lignes de journal portant `contestation_deposee` sont SUPPRIMÉES par la
    # descente, et c'est la seule façon de faire : le journal est en ajout seul, donc on
    # ne peut pas les réécrire sous un autre acte — il faudrait désactiver le déclencheur
    # pour les modifier, ce qui serait pire. Une descente qui efface une trace d'audit est
    # une opération à ne faire qu'en connaissance de cause, et c'est écrit ici.
    op.execute("DROP TRIGGER IF EXISTS journal_moderation_ajout_seul ON journal_moderation")
    op.execute(
        "DELETE FROM journal_moderation WHERE acte = 'contestation_deposee'"
    )
    op.execute("ALTER TYPE acte_moderation RENAME TO acte_moderation_avec_contestation")
    op.execute(
        "CREATE TYPE acte_moderation AS ENUM ("
        "'retrait_conservatoire', 'retrait_confirme', "
        "'retrait_annule', 'classement_sans_suite')"
    )
    op.execute(
        "ALTER TABLE journal_moderation ALTER COLUMN acte TYPE acte_moderation "
        "USING acte::text::acte_moderation"
    )
    op.execute("DROP TYPE acte_moderation_avec_contestation")
    op.execute(
        """
        CREATE TRIGGER journal_moderation_ajout_seul
            BEFORE UPDATE OR DELETE ON journal_moderation
            FOR EACH ROW EXECUTE FUNCTION journal_moderation_ajout_seul()
        """
    )
