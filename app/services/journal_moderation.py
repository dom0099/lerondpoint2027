"""Le journal d'audit de la modération. **Deux fonctions, et aucune des deux n'efface.**

Ce module est volontairement minuscule, et c'est son intérêt : il n'expose que
`ajouter` et `lire`. Il n'existe nulle part de `modifier`, de `supprimer` ni de
`corriger` — ni ici, ni dans une route, ni dans une commande. Un `grep -rn
"JournalModeration" app/` doit tenir sur un écran, et c'est ce qui rend la règle
vérifiable à l'œil.

La garantie ne repose cependant pas sur cette discipline : `journal_moderation` porte un
déclencheur PostgreSQL qui refuse tout `UPDATE` et tout `DELETE`, y compris depuis
`psql` (voir `app/models/moderation.py::JOURNAL_EN_AJOUT_SEUL`). Une fonction de service
ajoutée par distraction dans six mois ne pourrait donc pas écraser une ligne : elle
lèverait. `tests/test_signalement.py` le vérifie.

**Ce qui est journalisé dans ce lot**, et rien d'autre : le retrait conservatoire
automatique, la confirmation et l'annulation d'un retrait par le responsable, et le
classement sans suite d'un signalement.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActeModeration, JournalModeration

#: Les deux types de cible connus à ce jour. Une chaîne libre en base (voir le modèle),
#: mais nommée ici pour que deux appels n'écrivent pas « statement » et « proposition ».
CIBLE_PROPOSITION = "statement"
CIBLE_SIGNALEMENT = "signalement"


async def ajouter(
    session: AsyncSession,
    *,
    acte: ActeModeration,
    cible_type: str,
    cible_id: int,
    auteur: str,
    motif: str | None = None,
) -> JournalModeration:
    """Ajoute une ligne au journal. **Sans commit.**

    L'appelant commite, délibérément : le journal doit tomber dans la MÊME transaction
    que l'acte qu'il décrit. Commiter ici ferait exister des retraits non journalisés
    (si l'acte échoue après) ou des lignes de journal sans acte (s'il échoue avant) —
    c'est-à-dire, dans les deux cas, un journal qui ment.
    """
    ligne = JournalModeration(
        acte=acte,
        cible_type=cible_type,
        cible_id=cible_id,
        auteur=auteur,
        motif=motif,
    )
    session.add(ligne)
    return ligne


async def lire(
    session: AsyncSession,
    *,
    cible_type: str | None = None,
    cible_id: int | None = None,
    limite: int = 200,
) -> list[JournalModeration]:
    """Les actes journalisés, du plus récent au plus ancien.

    Filtrable sur une cible : « qu'est-il arrivé à cette proposition » est la question
    que pose quelqu'un qui conteste, et c'est la seule que ce journal doit savoir
    répondre vite.
    """
    requete = select(JournalModeration).order_by(JournalModeration.horodatage.desc())
    if cible_type is not None:
        requete = requete.where(JournalModeration.cible_type == cible_type)
    if cible_id is not None:
        requete = requete.where(JournalModeration.cible_id == cible_id)
    return list(await session.scalars(requete.limit(limite)))
