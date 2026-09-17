"""Routage des propositions par priorité.

Le poids de tirage est la RACINE de la priorité. red-dwarf calcule
`priorité = (importance × nouveauté)²` : la racine redonne donc exactement
`importance × nouveauté`, le score avant amplification. Plusieurs tests vérifient
cette propriété, parce que c'est elle qui justifie le dosage retenu.
"""

import math
import random
from collections import Counter

from sqlalchemy import select, update

from app.analysis import pipeline
from app.models import Conversation, Participant, StatementStat, Vote
from app.services import votes as votes_service
from app.services.votes import DEFAULT_WEIGHT, draw_weights, weight_for
from tests.conftest import open_conversation


# --- pondération, en isolation ------------------------------------------------------


def test_the_weight_is_the_unamplified_priority() -> None:
    """sqrt((importance × nouveauté)²) == importance × nouveauté."""
    # top_weight=0 pour neutraliser le plancher anti-exclusion, qui est testé à part.
    for score in (0.5, 2.0, 8.094, 27.711, 49.0):
        assert weight_for(score**2, top_weight=0.0) == score


def test_an_unrated_statement_takes_the_highest_weight() -> None:
    """Non notée n'est pas peu prioritaire : c'est inconnu, et red-dwarf lui
    donnerait de toute façon un facteur de nouveauté ×9."""
    poids = draw_weights([1, 2, 3], {1: 27.711, 2: 8.094, 3: None})

    assert poids[2] == max(poids)
    assert poids[2] == math.sqrt(27.711)


def test_a_conversation_without_any_score_draws_uniformly() -> None:
    poids = draw_weights([1, 2, 3], {})

    assert poids == [DEFAULT_WEIGHT] * 3


def test_equal_priorities_give_equal_weights() -> None:
    """Le cas courant, pas le cas d'école : des compteurs identiques donnent des
    scores identiques."""
    poids = draw_weights([1, 2, 3], {1: 8.094, 2: 8.094, 3: 8.094})

    assert len(set(poids)) == 1


def test_no_statement_is_ever_excluded_for_good() -> None:
    """Une priorité très basse doit ralentir la présentation, pas la supprimer :
    sinon la proposition ne récolte jamais les votes qui corrigeraient son score."""
    poids = draw_weights([1, 2], {1: 10_000.0, 2: 0.0})

    assert poids[1] > 0
    assert poids[1] == poids[0] * votes_service.MIN_WEIGHT_RATIO


def test_the_square_root_softens_the_gap() -> None:
    """Dosage retenu : atténuer pour ne pas laisser une poignée de propositions
    très clivantes monopoliser l'attention."""
    brut = 27.711 / 8.094
    attenue = math.sqrt(27.711) / math.sqrt(8.094)

    assert brut > 3.4
    assert 1.8 < attenue < 1.9


# --- tirage, contre la base ---------------------------------------------------------


async def _peupler(session, statements, effectif=10):
    participants = []
    for index in range(effectif):
        participant = Participant(anon_token=f"jeton-routage-{index}")
        session.add(participant)
        participants.append(participant)
    await session.flush()
    for index, participant in enumerate(participants):
        for position, statement_id in enumerate(statements):
            session.add(
                Vote(
                    participant_id=participant.id,
                    statement_id=statement_id,
                    value=1 if (position % 2 == index % 2) else -1,
                )
            )
    await session.commit()
    return participants


async def _tirer(session, conversation, participant, tirages=600, graine=7):
    rng = random.Random(graine)
    compte = Counter()
    for _ in range(tirages):
        choisie = await votes_service.next_statement(
            session, conversation, participant, rng=rng
        )
        compte[choisie.id] += 1
    return compte


async def test_without_any_analysis_the_draw_stays_uniform(
    moderator_client, client, session_factory
) -> None:
    """Comportement d'une conversation neuve : identique à l'ancien tirage au hasard."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=6
    )
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        observateur = Participant(anon_token="jeton-observateur")
        session.add(observateur)
        await session.commit()
        compte = await _tirer(session, conversation, observateur)

    attendu = 600 / len(statements)
    assert set(compte) == set(statements)
    assert all(0.6 * attendu < n < 1.6 * attendu for n in compte.values())


async def test_a_higher_priority_statement_is_drawn_more_often(
    moderator_client, session_factory
) -> None:
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        run = await pipeline.analyse(session, conversation)

        # Priorités choisies à la main pour que l'écart soit lisible.
        await session.execute(
            update(StatementStat)
            .where(StatementStat.run_id == run.id, StatementStat.group_id.is_(None))
            .values(priority=4.0)
        )
        await session.execute(
            update(StatementStat)
            .where(
                StatementStat.run_id == run.id,
                StatementStat.group_id.is_(None),
                StatementStat.statement_id == statements[0],
            )
            .values(priority=100.0)  # poids 10 contre 2
        )
        await session.commit()

        observateur = Participant(anon_token="jeton-priorite")
        session.add(observateur)
        await session.commit()
        compte = await _tirer(session, conversation, observateur)

    # Poids 10 contre 7×2 : environ 10/24 = 42 % des tirages.
    part = compte[statements[0]] / sum(compte.values())
    assert 0.33 < part < 0.51
    # Et aucune autre n'est écrasée.
    assert all(compte[sid] > 0 for sid in statements)


async def test_an_unrated_statement_overtakes_the_rated_ones(
    moderator_client, client, session_factory
) -> None:
    """Une proposition approuvée après le dernier calcul doit passer vite, pour
    accumuler les votes qui lui donneront un vrai score."""
    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        await pipeline.analyse(session, conversation)

    # Nouvelle proposition : elle n'existait pas au moment du calcul.
    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}/statements",
        data={"text": "Ajoutée après le calcul."},
        follow_redirects=False,
    )

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        priorites = await votes_service.latest_priorities(session, conversation)
        observateur = Participant(anon_token="jeton-nouvelle")
        session.add(observateur)
        await session.commit()
        compte = await _tirer(session, conversation, observateur)

    nouvelle = next(sid for sid in compte if sid not in priorites)
    notees = [compte[sid] for sid in statements]
    # Elle porte le poids MAXIMUM, donc à égalité avec la mieux notée — pas au-dessus.
    # On vérifie donc qu'elle est du même ordre que la tête, et nettement au-dessus
    # de la queue.
    assert compte[nouvelle] >= 0.7 * max(notees)
    assert compte[nouvelle] > 1.5 * min(notees)


async def test_a_priority_change_takes_effect_on_the_very_next_draw(
    moderator_client, session_factory
) -> None:
    """Pas de file précalculée : le tirage relit le dernier calcul à chaque appel."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        run = await pipeline.analyse(session, conversation)
        await session.execute(
            update(StatementStat)
            .where(StatementStat.run_id == run.id, StatementStat.group_id.is_(None))
            .values(priority=1.0)
        )
        await session.commit()

        observateur = Participant(anon_token="jeton-bascule")
        session.add(observateur)
        await session.commit()
        avant = await _tirer(session, conversation, observateur, tirages=400)

        # Une proposition devient soudain prioritaire.
        await session.execute(
            update(StatementStat)
            .where(
                StatementStat.run_id == run.id,
                StatementStat.group_id.is_(None),
                StatementStat.statement_id == statements[3],
            )
            .values(priority=400.0)  # poids 20 contre 1
        )
        await session.commit()
        apres = await _tirer(session, conversation, observateur, tirages=400)

    assert avant[statements[3]] < 100        # noyée dans l'uniforme
    assert apres[statements[3]] > 200        # devient dominante immédiatement


async def test_already_answered_statements_never_come_back(
    moderator_client, client, session_factory
) -> None:
    """Le participant qui a déjà répondu à tout ce qui est prioritaire reçoit le
    reste, pondéré entre elles — et jamais deux fois la même."""
    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=5
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        await pipeline.analyse(session, conversation)

    vues = []
    while True:
        etape = (await client.get(f"/api/conversations/{slug}/next-statement")).json()
        if etape["statement"] is None:
            break
        vues.append(etape["statement"]["id"])
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": etape["statement"]["id"], "value": 0},
        )

    assert sorted(vues) == sorted(statements)
    assert len(vues) == len(set(vues))
