"""Chantier Modération, MOD-3a — le signalement d'une proposition publiée.

**Additive et réversible, et rien d'autre** : deux tables créées, trois colonnes
nullables ajoutées, une valeur ajoutée à une énumération existante. Aucune colonne
supprimée, aucune donnée réécrite, aucun renommage. Les propositions déjà en base
sortent de cette migration exactement telles qu'elles y sont entrées, avec trois NULL de
plus — et aucune n'est retirée, puisque rien ne déclenche encore de retrait tant qu'un
signalement n'a pas été déposé.

**La valeur `retire` ÉTEND `moderation_status` au lieu de créer une seconde
énumération.** Deux listes parallèles finiraient par diverger, et surtout tout le site
filtre déjà sur `== approved` et jamais sur `!= rejected` (vérifié ligne à ligne avant
d'écrire ceci) : une valeur de plus sort donc la proposition de l'affichage, du tirage
pondéré et des API publiques **sans qu'aucune requête soit à reprendre**. C'est ce qui
rend le retrait sûr à si peu de frais.

Le type est partagé avec `conversation.moderation_status` et `group_naming.statut`, qui
gagnent donc eux aussi la valeur sans rien en faire. C'est le prix du partage, et il est
moins cher qu'une énumération jumelle.

**Le point délicat est le retour en arrière.** PostgreSQL sait ajouter une valeur à une
énumération, il ne sait pas en retirer une : le `downgrade` recrée donc le type sans
`retire`, ce qui oblige à détacher les trois colonnes qui le portent, l'index partiel
`ix_group_naming_valides` (dont le prédicat cite le type) et deux valeurs par défaut.
C'est la seule partie de ce fichier qui touche des données existantes, et elle ne le fait
que pour redescendre — voir le commentaire du `downgrade`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_signalement"
down_revision: str | None = "0017_video_de_presentation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `create_type=False` : le type existe depuis le chantier C, comme au 0016.
STATUT_MODERATION = sa.Enum(
    "pending", "approved", "rejected", "retire", name="moderation_status", create_type=False
)

STATUT_SIGNALEMENT = sa.Enum(
    "recu", "traite", "classe_sans_suite", name="statut_signalement"
)
ACTE_MODERATION = sa.Enum(
    "retrait_conservatoire",
    "retrait_confirme",
    "retrait_annule",
    "classement_sans_suite",
    name="acte_moderation",
)

#: Le déclencheur qui rend `journal_moderation` en ajout seul **au niveau du serveur**,
#: et pas seulement dans la discipline du code. Identique à `JOURNAL_EN_AJOUT_SEUL` de
#: `app/models/moderation.py`, qui le pose sur les bases construites par `create_all`
#: (les tests) ; ici, sur celles construites par les migrations (la production).
#:
#: Deux ordres et non un bloc : le pilote asyncpg refuse de préparer un énoncé qui en
#: contient plusieurs. La duplication avec le modèle est assumée — une migration qui
#: importerait le code applicatif se casserait le jour où celui-ci évolue, alors qu'une
#: migration décrit un état daté et doit rester rejouable telle quelle.
AJOUT_SEUL = (
    """
    CREATE OR REPLACE FUNCTION journal_moderation_ajout_seul() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'journal_moderation est en ajout seul : ni UPDATE ni DELETE';
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER journal_moderation_ajout_seul
        BEFORE UPDATE OR DELETE ON journal_moderation
        FOR EACH ROW EXECUTE FUNCTION journal_moderation_ajout_seul()
    """,
)


def upgrade() -> None:
    # `IF NOT EXISTS` pour que la migration se rejoue sans broncher sur une base où la
    # valeur aurait déjà été posée à la main. Depuis PostgreSQL 12, cet ordre s'exécute
    # dans une transaction ; la seule règle qui reste est de ne pas UTILISER la valeur
    # neuve avant la fin de la transaction — ce que cette migration ne fait pas.
    op.execute("ALTER TYPE moderation_status ADD VALUE IF NOT EXISTS 'retire'")

    op.add_column(
        "statement", sa.Column("retire_le", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("statement", sa.Column("retire_motif", sa.String(32), nullable=True))
    op.add_column("statement", sa.Column("retire_par", sa.String(160), nullable=True))

    op.create_table(
        "signalement",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "statement_id",
            sa.Integer(),
            sa.ForeignKey("statement.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # SET NULL et non CASCADE : la suppression d'un compte ne doit pas effacer les
        # signalements qu'il a émis, sans quoi il suffirait de supprimer son compte pour
        # faire disparaître ce qu'on a signalé — et le décompte s'en trouverait faussé.
        sa.Column(
            "participant_id",
            sa.Integer(),
            sa.ForeignKey("participant.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("motif_1", sa.String(32), nullable=False),
        sa.Column("motif_2", sa.String(32), nullable=True),
        sa.Column("texte_libre", sa.String(500), nullable=True),
        sa.Column("route", sa.String(32), nullable=False),
        sa.Column(
            "cree_le",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("referent_hache", sa.String(32), nullable=True),
        sa.Column("ip_hachee", sa.String(32), nullable=True),
        sa.Column(
            "statut", STATUT_SIGNALEMENT, server_default="recu", nullable=False
        ),
        sa.Column(
            "traite_par",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("traite_le", sa.DateTime(timezone=True), nullable=True),
        # Un seul signalement par (proposition, identité), porté par la base : une
        # vérification applicative laisserait passer deux requêtes concurrentes.
        sa.UniqueConstraint(
            "statement_id", "participant_id", name="uq_signalement_identite"
        ),
        sa.CheckConstraint(
            "motif_2 IS NULL OR motif_2 <> motif_1",
            name="ck_signalement_motifs_distincts",
        ),
    )
    op.create_index("ix_signalement_statement_id", "signalement", ["statement_id"])
    op.create_index("ix_signalement_participant_id", "signalement", ["participant_id"])
    op.create_index("ix_signalement_motif_1", "signalement", ["motif_1"])
    op.create_index("ix_signalement_route", "signalement", ["route"])
    op.create_index("ix_signalement_cree_le", "signalement", ["cree_le"])
    op.create_index("ix_signalement_statut", "signalement", ["statut"])
    # L'index de la file : « les signalements encore à traiter, les plus anciens
    # d'abord ». Partiel, comme `ix_group_naming_valides` : les lignes traitées et
    # classées ne sont jamais lues par ce chemin.
    op.create_index(
        "ix_signalement_a_traiter",
        "signalement",
        ["cree_le"],
        postgresql_where=sa.text("statut = 'recu'"),
    )

    # **Aucune clé étrangère sur le journal, et c'est délibéré** : un ON DELETE CASCADE
    # y supprimerait des lignes, un ON DELETE SET NULL en modifierait — dans les deux cas
    # la table cesserait d'être en ajout seul. `cible_id` et `auteur` sont des valeurs
    # recopiées, pas des liens.
    op.create_table(
        "journal_moderation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("acte", ACTE_MODERATION, nullable=False),
        sa.Column("cible_type", sa.String(32), nullable=False),
        sa.Column("cible_id", sa.Integer(), nullable=False),
        sa.Column("auteur", sa.String(160), nullable=False),
        sa.Column("motif", sa.String(500), nullable=True),
        sa.Column(
            "horodatage",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_journal_moderation_acte", "journal_moderation", ["acte"])
    op.create_index(
        "ix_journal_moderation_cible", "journal_moderation", ["cible_type", "cible_id"]
    )
    op.create_index(
        "ix_journal_moderation_horodatage", "journal_moderation", ["horodatage"]
    )
    for ordre in AJOUT_SEUL:
        op.execute(ordre)


def downgrade() -> None:
    # Le déclencheur d'abord : tant qu'il est là, la table refuse jusqu'à sa propre
    # suppression de lignes. `DROP TABLE` passerait, mais l'ordre inverse de la création
    # est plus lisible et ne dépend pas de ce détail.
    op.execute("DROP TRIGGER IF EXISTS journal_moderation_ajout_seul ON journal_moderation")
    op.execute("DROP FUNCTION IF EXISTS journal_moderation_ajout_seul()")
    op.drop_table("journal_moderation")
    op.drop_table("signalement")
    ACTE_MODERATION.drop(op.get_bind(), checkfirst=True)
    STATUT_SIGNALEMENT.drop(op.get_bind(), checkfirst=True)

    op.drop_column("statement", "retire_par")
    op.drop_column("statement", "retire_motif")
    op.drop_column("statement", "retire_le")

    # --- retirer la valeur `retire` de l'énumération ------------------------------
    #
    # PostgreSQL sait ajouter une valeur à une énumération, il ne sait pas en retirer
    # une : il faut donc recréer le type. C'est la seule partie de ce fichier qui touche
    # à des données existantes, et **c'est assumé pour une descente** : une proposition
    # retirée redevient `approved`, c'est-à-dire revient en circulation. Descendre cette
    # migration sur une base de production remettrait donc en ligne ce qui avait été
    # retiré — à ne faire qu'en connaissance de cause, et c'est écrit ici plutôt que
    # découvert après.
    #
    # L'index partiel `ix_group_naming_valides` est démonté puis remonté : son prédicat
    # cite le type (`statut = 'approved'::moderation_status`), et l'échange de type
    # échouerait en essayant de le reconstruire.
    op.execute("UPDATE statement SET moderation_status = 'approved' WHERE moderation_status = 'retire'")
    op.drop_index("ix_group_naming_valides", table_name="group_naming")
    op.execute("ALTER TABLE conversation ALTER COLUMN moderation_status DROP DEFAULT")
    op.execute("ALTER TABLE group_naming ALTER COLUMN statut DROP DEFAULT")
    op.execute("ALTER TYPE moderation_status RENAME TO moderation_status_avec_retire")
    op.execute("CREATE TYPE moderation_status AS ENUM ('pending', 'approved', 'rejected')")
    for table, colonne in (
        ("conversation", "moderation_status"),
        ("statement", "moderation_status"),
        ("group_naming", "statut"),
    ):
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {colonne} TYPE moderation_status "
            f"USING {colonne}::text::moderation_status"
        )
    op.execute("DROP TYPE moderation_status_avec_retire")
    op.execute(
        "ALTER TABLE conversation ALTER COLUMN moderation_status "
        "SET DEFAULT 'approved'::moderation_status"
    )
    op.execute(
        "ALTER TABLE group_naming ALTER COLUMN statut "
        "SET DEFAULT 'pending'::moderation_status"
    )
    op.create_index(
        "ix_group_naming_valides",
        "group_naming",
        ["conversation_id", "stable_group_id", "decided_at"],
        postgresql_where=sa.text("statut = 'approved'"),
    )
