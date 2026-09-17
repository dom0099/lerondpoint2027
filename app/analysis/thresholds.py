"""Seuil de participation à l'analyse : combien de votes pour être situé.

red-dwarf écarte de la matrice tout participant ayant moins de
`min_user_vote_threshold` votes ; son défaut est 7. Appliqué tel quel, ce défaut est
un blocage structurel sur une conversation jeune : `les-ecoles-du-quartier` compte
3 propositions approuvées, donc **aucun** participant ne peut atteindre 7 votes, et
cette conversation ne produirait jamais d'analyse — quel que soit le nombre de gens
qui y participent.

La borne ci-dessous rend la règle lisible — « avoir voté sur la majorité des
propositions » — sans abaisser le seuil des conversations mûres :

    seuil = min(7, max(2, ceil(nb_propositions × 0,6)))

      3 propositions ->  2      8 propositions ->  5
      5 propositions ->  3     11 propositions ->  7  (et au-delà : 7)

Le plancher de 2 évite l'autre absurdité : à une seule proposition, un vote unique
suffirait à « situer » quelqu'un dans un groupe d'opinion.

Un modérateur peut passer outre par conversation (`conversation.min_user_votes`), sur
le modèle de `force_group_count` — mais c'est la borne automatique qui corrige le
défaut, l'override n'est qu'un confort.
"""

import math

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Conversation, ModerationStatus, Statement

#: Part des propositions qu'il faut avoir vues. 0,6 = « la majorité », arrondi au
#: supérieur. Réglage, pas invariant : à ajuster une fois l'usage observé.
COVERAGE = 0.6

#: En dessous, être « situé » ne veut rien dire.
FLOOR = 2


def bound_for(n_statements: int) -> int:
    """Seuil automatique pour une conversation de `n_statements` propositions."""
    return min(
        settings.analysis_min_user_votes,
        max(FLOOR, math.ceil(n_statements * COVERAGE)),
    )


def effective_min_user_votes(conversation: Conversation, n_statements: int) -> int:
    """Seuil réellement appliqué : l'override du modérateur, sinon la borne."""
    if conversation.min_user_votes is not None:
        return conversation.min_user_votes
    return bound_for(n_statements)


async def approved_statement_count(
    session: AsyncSession, conversation: Conversation
) -> int:
    """Propositions approuvées — les seules qui entrent dans l'analyse."""
    return await session.scalar(
        select(func.count(Statement.id)).where(
            Statement.conversation_id == conversation.id,
            Statement.moderation_status == ModerationStatus.approved,
        )
    )


async def min_user_votes_for(
    session: AsyncSession, conversation: Conversation
) -> int:
    """Seuil de cette conversation, tel qu'il s'appliquera au prochain calcul."""
    return effective_min_user_votes(
        conversation, await approved_statement_count(session, conversation)
    )
