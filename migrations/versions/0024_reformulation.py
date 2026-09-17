"""Chantier Modération, MOD-10 — la navette de reformulation.

**Ce que la table porte, et pourquoi elle existe plutôt qu'une colonne de plus sur
`statement`.** Une reformulation est un échange : quelqu'un propose, l'auteur répond. Elle
a donc un émetteur, une date, un texte, une réponse et une date de réponse — et, surtout,
elle survit à son acceptation. Écrasée dans la proposition, il ne resterait que le
résultat, et c'est précisément ce qu'il ne faut pas perdre.

**`texte_origine` est la colonne qui rend la décision de dom tenable.** Les votes déjà
portés sur une proposition reformulée **restent valables** — c'est l'arbitrage du
15 septembre 2026. Il ne devient honnête qu'à une condition : que le texte sur lequel ces
gens ont réellement voté reste lisible quelque part. Il est donc recopié ici au moment de
la proposition, et non reconstruit après coup à partir du journal.

**Trois valeurs d'état, pas quatre.** `proposee`, `acceptee`, `refusee`. Pas de « caduque »
— personne ne la poserait, et un état que rien n'écrit finit par mentir. Une proposition
retirée entre-temps ne rend pas sa reformulation caduque en base : elle la rend
inatteignable, ce que la garde du service dit au moment où l'on essaie.

**Un index unique partiel** interdit deux reformulations en attente sur la même
proposition. Deux propositions concurrentes sur un même texte, c'est une élection — et une
élection demande le module d'agrégation de classements du MOD-9, donc plusieurs
reformulateurs, donc le tirage au sort qui n'existe pas encore (MOD-7, MOD-12). Tant
qu'il n'y a qu'un reformulateur, il n'y a qu'un candidat, et la base le tient.

(Le module n'est nommé nulle part dans ce lot : son test-sentinelle cherche le mot, y
compris en commentaire, et il a raison de le faire.)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_reformulation"
down_revision: str | None = "0023_rappel_reformulation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Déclaré en constante et **laissé créer par `create_table`**, comme `statut_signalement`
#: au 0018. Un `create()` explicite en plus fait tenter la création deux fois, et la
#: descente ne sait plus quoi supprimer : c'est l'aller-retour du test des migrations qui
#: l'a dit, pas une relecture.
STATUT_REFORMULATION = sa.Enum(
    "proposee", "acceptee", "refusee", name="statut_reformulation"
)


def upgrade() -> None:
    # Comme pour `retire` au 0018 et `contestation_deposee` au 0019 : depuis
    # PostgreSQL 12 cet ordre s'exécute dans une transaction, la seule règle étant de ne
    # pas UTILISER la valeur neuve avant la fin de celle-ci. Cette migration ne
    # l'utilise pas — elle crée une table, elle n'écrit aucun acte.
    for acte in (
        "reformulation_proposee",
        "reformulation_acceptee",
        "reformulation_refusee",
    ):
        op.execute(f"ALTER TYPE acte_moderation ADD VALUE IF NOT EXISTS '{acte}'")

    op.create_table(
        "reformulation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "statement_id",
            sa.Integer(),
            sa.ForeignKey("statement.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # Le texte tel qu'il était AU MOMENT DE LA PROPOSITION. C'est celui sur lequel
        # les votes conservés ont réellement porté.
        sa.Column("texte_origine", sa.Text(), nullable=False),
        sa.Column("texte_propose", sa.String(500), nullable=False),
        # `SET NULL` et non `CASCADE` : la suppression d'un compte de modération ne doit
        # pas effacer l'échange qu'il a ouvert. Le journal d'audit, lui, garde l'adresse
        # en clair — l'un sert à joindre, l'autre à rester lisible.
        sa.Column(
            "propose_par",
            sa.Uuid(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "propose_le",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "statut",
            STATUT_REFORMULATION,
            server_default="proposee",
            nullable=False,
        ),
        sa.Column("repondu_le", sa.DateTime(timezone=True), nullable=True),
    )

    # Une seule reformulation EN ATTENTE par proposition. Partiel, comme
    # `ix_signalement_a_traiter` : les échanges clos ne sont jamais lus par ce chemin, et
    # rien n'interdit qu'une proposition en ait connu plusieurs à la suite.
    op.create_index(
        "uq_reformulation_en_attente",
        "reformulation",
        ["statement_id"],
        unique=True,
        postgresql_where=sa.text("statut = 'proposee'"),
    )


def downgrade() -> None:
    op.drop_index("uq_reformulation_en_attente", table_name="reformulation")
    op.drop_table("reformulation")
    STATUT_REFORMULATION.drop(op.get_bind(), checkfirst=True)
    # Les trois valeurs ajoutées à `acte_moderation` NE SONT PAS retirées, et c'est
    # volontaire : PostgreSQL ne sait pas défaire un ADD VALUE sans recréer le type, et
    # le journal d'audit est en ajout seul — une descente qui devrait réécrire des actes
    # déjà journalisés pour pouvoir supprimer leur valeur d'énumération ferait
    # exactement ce que la table interdit. Une valeur inutilisée ne coûte rien.
