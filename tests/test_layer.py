"""Couche intermédiaire : persistance, réamorçage, cache vidé, concurrence.

Tests d'intégration du chantier D. Les tests unitaires de la mécanique elle-même sont
dans `test_buckets.py` et `test_smoothing.py`.
"""

import numpy as np
import pytest
from sqlalchemy import select, text

from app.analysis import pipeline
from app.analysis.staged import _niveau_groupe
from app.models import (
    AnalysisStatus,
    BaseCluster,
    BaseClusterMember,
    Conversation,
    Participant,
    ParticipantProjection,
    Vote,
)
from tests.conftest import open_conversation


async def _populate(session, statement_ids, n_participants=10):
    """Deux camps, mais aucun profil de vote strictement dupliqué.

    La nuance compte : des participants aux votes RIGOUREUSEMENT identiques se
    projettent sur le même point, et la fusion de la couche les réunit en un seul
    seau — comportement voulu (`uniqify-clusters` chez Pol.is), mais qui ne
    permettrait pas d'observer la continuité des identifiants de seaux. Chacun
    diverge donc de son camp sur une proposition qui lui est propre.
    """
    participants = []
    for index in range(n_participants):
        participant = Participant(anon_token=f"jeton-couche-{index}")
        session.add(participant)
        participants.append(participant)
    await session.flush()

    for index, participant in enumerate(participants):
        camp = index % 2
        singuliere = index % len(statement_ids)
        for position, statement_id in enumerate(statement_ids):
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


async def _seaux(session, conversation_id):
    rows = await session.execute(
        select(BaseCluster.bucket_id, BaseClusterMember.participant_id)
        .join(BaseClusterMember, BaseClusterMember.base_cluster_id == BaseCluster.id)
        .where(BaseCluster.conversation_id == conversation_id)
    )
    couche: dict[int, set[int]] = {}
    for bucket_id, participant_id in rows:
        couche.setdefault(bucket_id, set()).add(participant_id)
    return couche


async def test_the_layer_is_persisted_and_reused(
    moderator_client, session_factory
) -> None:
    """Le cache existe après un calcul, et le calcul suivant repart dessus."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=10
    )

    async with session_factory() as session:
        await _populate(session, statements)
        conversation = await session.get(Conversation, conversation_id)

        premier = await pipeline.analyse(session, conversation)
        assert premier.status == AnalysisStatus.ok
        couche_1 = await _seaux(session, conversation_id)
        assert couche_1, "aucun seau persisté"

        # Le découpage groupe et les compteurs du lisseur sont là aussi.
        await session.refresh(conversation)
        assert conversation.group_partition
        assert conversation.smoother_smoothed_k == premier.k

        second = await pipeline.analyse(session, conversation)
        assert second.status == AnalysisStatus.ok
        couche_2 = await _seaux(session, conversation_id)

    # Données inchangées : les seaux gardent leurs identifiants ET leur composition.
    assert couche_2 == couche_1
    # La trace du lisseur est consignée sur le run, pour pouvoir l'expliquer.
    assert "couche" in (second.params or {})


async def test_an_incoherent_layer_is_dropped_and_the_run_still_succeeds(
    moderator_client, session_factory
) -> None:
    """Le cache est un cache : on le vide plutôt que de propager une corruption.

    On fabrique l'incohérence la plus grave — un même participant dans deux seaux,
    donc une couche qui ne partitionne plus rien.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=10
    )

    async with session_factory() as session:
        participants = await _populate(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        await pipeline.analyse(session, conversation)

        seaux = list(
            await session.scalars(
                select(BaseCluster).where(BaseCluster.conversation_id == conversation_id)
            )
        )
        assert len(seaux) >= 2
        # Le participant du premier seau est ajouté de force au deuxième.
        intrus = await session.scalar(
            select(BaseClusterMember.participant_id).where(
                BaseClusterMember.base_cluster_id == seaux[0].id
            )
        )
        session.add(
            BaseClusterMember(base_cluster_id=seaux[1].id, participant_id=intrus)
        )
        await session.commit()

        run = await pipeline.analyse(session, conversation)
        assert run.status == AnalysisStatus.ok      # le tour aboutit quand même
        couche = await _seaux(session, conversation_id)

    # Le cache a été reconstruit, et il partitionne de nouveau.
    vus: list[int] = []
    for membres in couche.values():
        vus.extend(membres)
    assert len(vus) == len(set(vus))
    assert len(vus) == len(participants)


async def test_a_concurrent_analysis_fails_cleanly(
    moderator_client, session_factory
) -> None:
    """Un calcul manuel pendant le passage horaire échoue au lieu de corrompre.

    Sans ce verrou, deux calculs simultanés réécriraient tous les deux la couche : le
    perdant laisserait un état qui ne correspond à aucun run.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=10
    )

    async with session_factory() as session:
        await _populate(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        premier = await pipeline.analyse(session, conversation)
        couche_avant = await _seaux(session, conversation_id)

    async with session_factory() as bloqueur:
        # Un autre calcul tient la conversation.
        await bloqueur.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :i)"),
            {"ns": pipeline.VERROU_ANALYSE, "i": conversation_id},
        )
        async with session_factory() as session:
            conversation = await session.get(Conversation, conversation_id)
            run = await pipeline.analyse(session, conversation)
        await bloqueur.rollback()

    assert run.status == AnalysisStatus.error
    assert "déjà en cours" in run.error_text

    async with session_factory() as session:
        # L'état n'a pas bougé, et le dernier run abouti reste le premier.
        assert await _seaux(session, conversation_id) == couche_avant
        dernier = await pipeline.latest_ok_run(
            session, await session.get(Conversation, conversation_id)
        )
        assert dernier.id == premier.id


def test_the_group_level_honours_the_bucket_weights() -> None:
    """Le poids d'un seau doit peser sur le centre du groupe.

    Sans `sample_weight`, un seau de 50 personnes compterait autant qu'un seau d'une
    seule : toute la couche intermédiaire perdrait son sens.
    """
    centres = np.array([[0.0, 0.0], [1.0, 0.0], [10.0, 0.0]])

    appartenances = [{1}, {2}, {3}]
    positions = {
        1: np.array([0.0, 0.0]),
        2: np.array([1.0, 0.0]),
        3: np.array([10.0, 0.0]),
    }

    egaux = _niveau_groupe(
        centres,
        np.array([1.0, 1.0, 1.0]),
        appartenances,
        positions,
        None,
        force_k=2,
        random_state=42,
    )
    penches = _niveau_groupe(
        centres,
        np.array([9.0, 1.0, 1.0]),
        appartenances,
        positions,
        None,
        force_k=2,
        random_state=42,
    )

    def centre_du_groupe_gauche(modeles):
        modele = modeles[2][1]
        # Le groupe qui contient les deux points de gauche.
        etiquette = modele.labels_[0]
        assert modele.labels_[1] == etiquette
        return modele.cluster_centers_[etiquette][0]

    assert centre_du_groupe_gauche(egaux) == pytest.approx(0.5)    # moyenne simple
    assert centre_du_groupe_gauche(penches) == pytest.approx(0.1)  # tiré par le lourd


async def test_the_layer_survives_a_participant_leaving(
    moderator_client, session_factory
) -> None:
    """Un participant supprimé ne doit pas figer la couche (cascade + recentrage)."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=10
    )

    async with session_factory() as session:
        participants = await _populate(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        await pipeline.analyse(session, conversation)

        partant = participants[0]
        await session.execute(
            text("DELETE FROM vote WHERE participant_id = :p"), {"p": partant.id}
        )
        await session.delete(await session.get(Participant, partant.id))
        await session.commit()

        run = await pipeline.analyse(session, conversation)
        assert run.status == AnalysisStatus.ok

        restants = await session.scalars(
            select(ParticipantProjection.participant_id).where(
                ParticipantProjection.run_id == run.id
            )
        )
        assert partant.id not in set(restants)
