"""Carte : positions individuelles et contours de groupe (chantier D4).

Point d'export en lecture seule. Ce qui est vérifié ici tient en quatre exigences :
une position sous le seuil ne sort jamais, un groupe de moins de trois points n'a pas
de contour et n'est pas une erreur, rien ne fuit d'un run à l'autre, et le visiteur
reçoit un état qui distingue « pas assez voté » de « pas encore recalculé ».
"""

import numpy as np
import pytest
from sqlalchemy import select

from app.analysis import pipeline
from app.config import settings
from app.models import Conversation, Participant, ParticipantProjection, Vote
from app.services import carte as carte_service
from tests.conftest import open_conversation


async def _peupler(session, statements, effectif=10, prefixe="carte"):
    """Deux camps francs, mais chacun s'écarte sur une proposition qui lui est propre.

    Sans cet écart, tous les membres d'un camp se projettent au même point : le groupe
    n'a alors qu'un seul point distinct, et il n'y a par construction aucun contour à
    tracer. On ne testerait plus les enveloppes, seulement leur absence.
    """
    participants = []
    for index in range(effectif):
        participant = Participant(anon_token=f"jeton-{prefixe}-{index}")
        session.add(participant)
        participants.append(participant)
    await session.flush()

    for index, participant in enumerate(participants):
        camp = index % 2
        singuliere = index % len(statements)
        for position, statement_id in enumerate(statements):
            valeur = 1 if (position % 2 == camp) else -1
            if position == singuliere:
                valeur = -valeur
            session.add(
                Vote(
                    participant_id=participant.id,
                    statement_id=statement_id,
                    value=valeur,
                )
            )
    await session.commit()
    return participants


# --- l'enveloppe elle-même ---------------------------------------------------------


def test_a_hull_needs_three_points() -> None:
    """Deux points ne délimitent pas une surface : pas de contour, pas d'erreur."""
    assert carte_service.enveloppe_concave(np.array([[0.0, 0.0]])) is None
    assert carte_service.enveloppe_concave(np.array([[0.0, 0.0], [1.0, 1.0]])) is None


def test_identical_points_never_reach_the_c_extension() -> None:
    """Le garde-fou qui empêche une erreur de segmentation.

    `concave_hull` est une extension C++ : sur un tableau dont tous les points sont
    confondus, elle ne lève pas, elle fait tomber le processus (code 139). Aucun `try`
    ne rattrape cela. Trois personnes ayant voté à l'identique, c'est exactement cette
    entrée — le seul remède est de ne jamais appeler la bibliothèque dessus.
    """
    confondus = np.array([[1.0, 1.0], [1.0, 1.0], [1.0, 1.0]])
    assert carte_service.enveloppe_concave(confondus) is None

    # Deux points distincts et un doublon : encore trop peu de points DISTINCTS.
    assert carte_service.enveloppe_concave(
        np.array([[0.0, 0.0], [0.0, 0.0], [1.0, 1.0]])
    ) is None


def test_collinear_points_yield_no_hull() -> None:
    """Trois points alignés : la bibliothèque rend 2 sommets, donc aucune surface."""
    alignes = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]])
    assert carte_service.enveloppe_concave(alignes) is None


def test_a_real_hull_is_a_closed_set_of_actual_points() -> None:
    """Les sommets sont des positions RÉELLES de participants, pas des centres."""
    points = np.array([[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0], [1.0, 1.0]])
    sommets = carte_service.enveloppe_concave(points)

    assert sommets is not None and len(sommets) >= 3
    connus = {tuple(p) for p in points.tolist()}
    for sommet in sommets:
        assert tuple(sommet) in connus, f"{sommet} n'est la position de personne"
    # Le point intérieur n'a rien à faire sur le contour.
    assert [1.0, 1.0] not in sommets


def test_the_concavity_is_a_setting_not_a_constant(monkeypatch) -> None:
    """red-dwarf fige 4.0 ; ici c'est un réglage, et il est réellement lu."""
    rng = np.random.default_rng(0)
    points = np.vstack([rng.normal(size=(40, 2)), rng.normal(loc=6, size=(40, 2))])

    monkeypatch.setattr(settings, "hull_concavity", 0.1)
    epousant = carte_service.enveloppe_concave(points)
    monkeypatch.setattr(settings, "hull_concavity", 100.0)
    lisse = carte_service.enveloppe_concave(points)

    assert epousant is not None and lisse is not None
    # Plus la concavité est faible, plus le contour épouse les creux : il lui faut
    # davantage de sommets. Sans lecture du réglage, les deux seraient identiques.
    assert len(epousant) > len(lisse)


# --- la carte complète -------------------------------------------------------------


async def test_nothing_before_the_first_computation(moderator_client, client) -> None:
    _, slug, _ = await open_conversation(moderator_client)
    assert (await client.get(f"/api/conversations/{slug}/carte")).json() is None


async def test_the_map_exposes_positions_and_hulls(
    moderator_client, client, session_factory
) -> None:
    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2
        await session.commit()
        run = await pipeline.analyse(session, conversation)

    carte = (await client.get(f"/api/conversations/{slug}/carte")).json()

    assert carte["run_id"] == run.id
    assert carte["computed_at"]
    assert len(carte["positions"]) == 10
    assert {g["name"] for g in carte["groupes"]} == {"A", "B"}
    assert sum(g["size"] for g in carte["groupes"]) == 10
    for groupe in carte["groupes"]:
        assert groupe["sommets"], f"le groupe {groupe['name']} n'a pas de contour"
        assert len(groupe["sommets"]) >= 3

    # Les sommets d'un contour sont des positions réellement présentes sur la carte.
    connus = {(round(p["x"], 9), round(p["y"], 9)) for p in carte["positions"]}
    for groupe in carte["groupes"]:
        for x, y in groupe["sommets"]:
            assert (round(x, 9), round(y, 9)) in connus


async def test_no_identifiers_are_attached_to_the_dots(
    moderator_client, client, session_factory
) -> None:
    """La carte est une nuée anonyme : aucun point ne nomme quelqu'un."""
    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        await pipeline.analyse(session, conversation)

    carte = (await client.get(f"/api/conversations/{slug}/carte")).json()
    for point in carte["positions"]:
        assert set(point) == {"x", "y", "groupe"}


# --- le filtrage sous seuil --------------------------------------------------------


async def test_a_position_below_the_threshold_is_never_exposed(
    moderator_client, client, session_factory
) -> None:
    """Elle existe en base, elle ne sort pas de l'API — ni dans la nuée, ni pour soi."""
    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        await pipeline.analyse(session, conversation)

    # Ce visiteur vote une seule fois : sous le seuil de cette conversation (5).
    detail = (await client.get(f"/api/conversations/{slug}")).json()
    await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": detail["statements"][0]["id"], "value": 1},
    )
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        run = await pipeline.analyse(session, conversation)

        # La position EXISTE bien en base, sans groupe : c'est ce qu'on refuse d'exposer.
        sous_seuil = (
            await session.execute(
                select(ParticipantProjection.x, ParticipantProjection.y).where(
                    ParticipantProjection.run_id == run.id,
                    ParticipantProjection.stable_group_id.is_(None),
                )
            )
        ).all()
    assert sous_seuil, "le banc ne teste rien : personne n'est sous le seuil"

    carte = (await client.get(f"/api/conversations/{slug}/carte")).json()

    assert len(carte["positions"]) == 10  # les 10 peuplés, pas le onzième
    exposees = {(round(p["x"], 9), round(p["y"], 9)) for p in carte["positions"]}
    for x, y in sous_seuil:
        assert (round(x, 9), round(y, 9)) not in exposees

    # Et le visiteur reçoit un état, jamais ses coordonnées.
    assert carte["visiteur"]["etat"] == "sous_le_seuil"
    assert carte["visiteur"]["x"] is None and carte["visiteur"]["y"] is None
    assert "5 votes" in carte["visiteur"]["raison"]


async def test_the_visitor_who_voted_after_the_run_is_told_to_wait(
    moderator_client, client, session_factory
) -> None:
    """Le seul « gris » réel, et il est temporel : assez de votes, mais trop tard.

    Ce cas ne doit pas recevoir le message « il vous faut N votes » — la personne les a
    déjà émis. Lui redemander de voter serait une consigne fausse.
    """
    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        await pipeline.analyse(session, conversation)

    # Le visiteur vote assez, mais APRÈS le calcul : aucun recalcul derrière.
    detail = (await client.get(f"/api/conversations/{slug}")).json()
    for position, statement in enumerate(detail["statements"]):
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement["id"], "value": 1 if position % 2 else -1},
        )

    carte = (await client.get(f"/api/conversations/{slug}/carte")).json()

    assert carte["visiteur"]["etat"] == "en_attente_de_calcul"
    assert carte["visiteur"]["x"] is None
    assert "prochain" in carte["visiteur"]["raison"].lower()


async def test_the_visitor_who_is_placed_gets_their_own_position(
    moderator_client, client, session_factory
) -> None:
    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2
        await session.commit()
        await pipeline.analyse(session, conversation)

    detail = (await client.get(f"/api/conversations/{slug}")).json()
    for position, statement in enumerate(detail["statements"]):
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement["id"], "value": 1 if position % 2 == 0 else -1},
        )
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        await pipeline.analyse(session, conversation)

    carte = (await client.get(f"/api/conversations/{slug}/carte")).json()
    visiteur = carte["visiteur"]

    assert visiteur["etat"] == "situe"
    assert visiteur["groupe"] in {"A", "B"}
    assert visiteur["raison"] is None
    # Sa position est bien l'une de celles de la carte, pas une valeur à part.
    exposees = {(round(p["x"], 9), round(p["y"], 9)) for p in carte["positions"]}
    assert (round(visiteur["x"], 9), round(visiteur["y"], 9)) in exposees


# --- un groupe trop petit pour un contour ------------------------------------------


async def test_a_group_smaller_than_three_has_no_hull_and_no_error(
    moderator_client, client, session_factory
) -> None:
    """Le contour manque, la carte sort quand même, et le groupe reste compté."""
    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        run = await pipeline.analyse(session, conversation)

        # On isole deux personnes dans un groupe à elles : moins de 3 points.
        lignes = list(
            await session.scalars(
                select(ParticipantProjection)
                .where(ParticipantProjection.run_id == run.id)
                .limit(2)
            )
        )
        seul = max(
            p.stable_group_id
            for p in await session.scalars(
                select(ParticipantProjection).where(
                    ParticipantProjection.run_id == run.id
                )
            )
        ) + 1
        for ligne in lignes:
            ligne.stable_group_id = seul
        await session.commit()

    reponse = await client.get(f"/api/conversations/{slug}/carte")
    assert reponse.status_code == 200

    carte = reponse.json()
    petit = [g for g in carte["groupes"] if g["size"] == 2]
    assert petit, "le banc n'a pas produit de petit groupe"
    assert petit[0]["sommets"] is None
    # Ses membres restent des points de la carte : c'est le CONTOUR qui manque,
    # pas les personnes.
    assert len(carte["positions"]) == 10


# --- étanchéité entre runs ---------------------------------------------------------


async def test_the_map_never_mixes_two_runs(
    moderator_client, client, session_factory
) -> None:
    """Le repère PCA se déplace d'un calcul à l'autre : mélanger deux runs
    dessinerait un mouvement qui n'a pas eu lieu."""
    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        premier = await pipeline.analyse(session, conversation)

    # Deuxième calcul, avec des votes en plus : un run distinct, et des positions
    # qui ne sont plus dans le même repère.
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        await _peupler(session, statements, effectif=6, prefixe="second")
        second = await pipeline.analyse(session, conversation)
    assert second.id != premier.id

    carte = (await client.get(f"/api/conversations/{slug}/carte")).json()
    assert carte["run_id"] == second.id

    async with session_factory() as session:
        anciennes = {
            (round(x, 9), round(y, 9))
            for x, y in (
                await session.execute(
                    select(ParticipantProjection.x, ParticipantProjection.y).where(
                        ParticipantProjection.run_id == premier.id
                    )
                )
            ).all()
        }
        nouvelles = {
            (round(x, 9), round(y, 9))
            for x, y in (
                await session.execute(
                    select(ParticipantProjection.x, ParticipantProjection.y).where(
                        ParticipantProjection.run_id == second.id
                    )
                )
            ).all()
        }

    exposees = {(round(p["x"], 9), round(p["y"], 9)) for p in carte["positions"]}
    assert exposees <= nouvelles, "la carte expose des points hors du run courant"
    # Le premier run a bien produit des positions que la carte ne montre pas.
    assert anciennes - nouvelles, "les deux runs sont identiques : le test ne prouve rien"
    assert not (exposees & (anciennes - nouvelles))
