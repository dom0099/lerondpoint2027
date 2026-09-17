"""Chantier Modération, MOD-4 — la validation aléatoire par les participants.

**Ce lot ne rétablit pas la grille de relecture supprimée au MOD-14.** Il porte le même
numéro parce que dom a recadré le lot le 15 septembre 2026 au soir, et il fait le
contraire : la grille était une case à cocher par le responsable, seul, et elle a rendu
zéro ligne en trois semaines ; celle-ci est une question posée au hasard aux participants,
et c'est eux qui l'alimentent. Voir §14.3 de `chantier-moderation-plan-v2.md`.

**Deux changements, et le second est le plus important des deux.**

1. La table `validation` : qui a regardé quoi, et ce qu'il en a dit — « conforme » ou
   « à revoir ». Les DEUX verdicts sont enregistrés. Ne garder que les « à revoir »
   ferait une table de plaintes de plus, sans aucun des taux d'accord dont le MOD-6 a
   besoin pour habiliter qui que ce soit.

2. La colonne `signalement.sollicite`. Un « à revoir » engendre un signalement ordinaire,
   qui suit le chemin habituel — ligne rouge → retrait conservatoire, mal formulé →
   reformulation. Mais ce signalement-là a été **provoqué par le site**, et rien ne
   permettrait de le distinguer d'une plainte spontanée. L'indice d'indépendance du MOD-5
   compterait alors comme afflux coordonné ce que le site a lui-même déclenché : cinq
   validations sollicitées sur une même proposition, dans la même heure, par cinq
   identités fraîches qui n'ont pas voté ce débat, c'est mot pour mot la signature que
   ses cinq indicateurs cherchent. **La détection de tempête se déclencherait sur son
   propre sondage.**

   `server_default='false'` : tous les signalements déjà en base sont spontanés, puisque
   rien ne sollicitait personne avant aujourd'hui. Le défaut dit la vérité du passé, il
   ne la suppose pas.

**Aucune donnée de contexte sur `validation`** — ni référent, ni empreinte d'adresse,
contrairement à `signalement`. Elles n'y serviraient à rien : une validation ne peut pas
être coordonnée, puisque c'est le site qui choisit à qui il la propose et sur quoi. Deux
colonnes de plus « au cas où » seraient précisément ce que le §7 du MOD-5 s'interdisait,
et il faudrait les purger à trente jours comme les autres.

**Aucun acte n'est ajouté à `acte_moderation`.** Lire n'est pas un acte — c'est la règle
posée au MOD-13. Ce qui se journalise, c'est le retrait qu'un « à revoir » déclenche le
cas échéant, et il l'est déjà par `signalement_file._retirer`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_validation"
down_revision: str | None = "0024_reformulation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Déclaré en constante et **laissé créer par `create_table`**, comme
#: `statut_reformulation` au 0024 et `statut_signalement` au 0018. Un `create()` explicite
#: en plus ferait tenter la création deux fois, et la descente ne saurait plus quoi
#: supprimer : c'est l'aller-retour du test des migrations qui l'a appris, pas une
#: relecture.
VERDICT_VALIDATION = sa.Enum("conforme", "a_revoir", name="verdict_validation")


def upgrade() -> None:
    op.add_column(
        "signalement",
        sa.Column(
            "sollicite",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    # Indexée parce que le MOD-5 filtre dessus à chaque lecture d'un débat surveillé, et
    # que le rapport du responsable compte les deux populations séparément.
    op.create_index("ix_signalement_sollicite", "signalement", ["sollicite"])

    op.create_table(
        "validation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "statement_id",
            sa.Integer(),
            sa.ForeignKey("statement.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # `SET NULL` et non `CASCADE` : la suppression d'un compte ne doit pas effacer
        # les jugements qu'il a rendus, sans quoi le taux d'accord d'un débat se
        # réécrirait tout seul le jour où quelqu'un s'en va.
        sa.Column(
            "participant_id",
            sa.Integer(),
            sa.ForeignKey("participant.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("verdict", VERDICT_VALIDATION, nullable=False),
        sa.Column(
            "cree_le",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Une seule validation par (proposition, identité). Portée par la BASE : un
        # double clic sur « Conforme » compterait sinon deux avis.
        sa.UniqueConstraint(
            "statement_id", "participant_id", name="uq_validation_identite"
        ),
    )
    # Le tirage demande « combien de validations porte déjà cette proposition » à chaque
    # carte servie ; le plafond par personne demande « combien depuis 24 h ».
    op.create_index("ix_validation_proposition", "validation", ["statement_id"])
    op.create_index(
        "ix_validation_identite_date", "validation", ["participant_id", "cree_le"]
    )
    op.create_index("ix_validation_verdict", "validation", ["verdict"])
    op.create_index("ix_validation_cree_le", "validation", ["cree_le"])


def downgrade() -> None:
    op.drop_index("ix_validation_cree_le", table_name="validation")
    op.drop_index("ix_validation_verdict", table_name="validation")
    op.drop_index("ix_validation_identite_date", table_name="validation")
    op.drop_index("ix_validation_proposition", table_name="validation")
    op.drop_table("validation")
    VERDICT_VALIDATION.drop(op.get_bind(), checkfirst=True)
    op.drop_index("ix_signalement_sollicite", table_name="signalement")
    op.drop_column("signalement", "sollicite")
