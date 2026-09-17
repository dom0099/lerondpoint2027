"""Chantier Vidéo, VIDEO-1 — la vidéo de présentation d'un débat.

Quatre colonnes sur `conversation`, toutes NULLABLES, aucune reprise de données, aucune
valeur par défaut : les débats existants sortent de cette migration exactement tels
qu'ils y sont entrés, avec quatre NULL de plus. Rien ne les lit encore — aucun gabarit,
aucun routeur — et c'est voulu : ce lot construit le mécanisme, le VIDEO-2 le branchera.

**Ce qui n'entre PAS ici : les octets de la vidéo.** `video_chemin` et
`video_miniature_chemin` portent des chemins relatifs à `settings.video_racine`, sur le
modèle exact des liens-source du chantier J — une adresse en base, la ressource ailleurs.
Mettre un `.mp4` de 2,5 Mo dans une colonne ferait grossir chaque sauvegarde de la base
d'autant, et ferait passer chaque lecture de débat par le transfert d'un binaire que
personne ne demandait.

Relatifs, et non absolus : le jour où le VPS monte un disque dédié aux médias, il n'y a
qu'une variable d'environnement à changer — pas une colonne à réécrire ligne à ligne.

`video_duree_secondes` est en `Float` et non en entier : la durée est celle que ffprobe
MESURE, et elle vaut 24,72 s bien plus souvent que 25. L'arrondir à la réception ferait
perdre la seule mesure qui a servi à accepter le fichier.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_video_de_presentation"
down_revision: str | None = "0016_nommage_moderation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversation", sa.Column("video_chemin", sa.String(255), nullable=True))
    op.add_column(
        "conversation", sa.Column("video_miniature_chemin", sa.String(255), nullable=True)
    )
    op.add_column(
        "conversation", sa.Column("video_duree_secondes", sa.Float(), nullable=True)
    )
    op.add_column("conversation", sa.Column("video_profil", sa.String(32), nullable=True))
    # Aucun index. On ne cherche jamais un débat PAR sa vidéo : on lit un débat, et on
    # regarde s'il en a une. Un index ici ne servirait qu'à ralentir les écritures.


def downgrade() -> None:
    op.drop_column("conversation", "video_profil")
    op.drop_column("conversation", "video_duree_secondes")
    op.drop_column("conversation", "video_miniature_chemin")
    op.drop_column("conversation", "video_chemin")
    # Les fichiers déjà écrits sur le disque ne sont PAS effacés : une migration qui
    # supprime des colonnes doit pouvoir se rejouer à l'envers sans détruire ce qu'elle
    # n'a pas créé. Le ménage du disque, s'il faut le faire, est un geste séparé.
