"""Analyse red-dwarf : extraction, seuils, exécution et persistance."""

from sqlalchemy import func, select

from app.analysis import pipeline
from app.config import settings
from app.models import (
    AnalysisStatus,
    Conversation,
    ModerationStatus,
    ParticipantProjection,
    Participant,
    Statement,
    StatementStat,
    Vote,
)
from tests.conftest import depublier, open_conversation


async def _populate(
    session, conversation_id, statement_ids, n_participants=6, prefixe="test"
):
    """Deux camps nets, pour que le clustering ait quelque chose à trouver.

    `prefixe` distingue les jetons : un test qui peuple DEUX débats — l'onglet
    consensuel du L6 en a besoin — buterait sinon sur l'unicité de `anon_token`.
    """
    participants = []
    for index in range(n_participants):
        participant = Participant(anon_token=f"jeton-{prefixe}-{index}")
        session.add(participant)
        participants.append(participant)
    await session.flush()

    for index, participant in enumerate(participants):
        camp = index % 2
        for position, statement_id in enumerate(statement_ids):
            session.add(
                Vote(
                    participant_id=participant.id,
                    statement_id=statement_id,
                    value=1 if (position % 2 == camp) else -1,
                )
            )
    await session.commit()
    return participants


async def test_extraction_does_not_invert_the_sign(
    moderator_client, session_factory
) -> None:
    """Le point le plus coûteux du chantier B, verrouillé par un test.

    Pol.is stocke agree = -1 ; nous stockons agree = +1, comme red-dwarf. Une
    inversion à l'extraction produirait des groupes en miroir SANS aucune erreur.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=2
    )

    async with session_factory() as session:
        participant = Participant(anon_token="jeton-signe")
        session.add(participant)
        await session.flush()
        session.add(
            Vote(participant_id=participant.id, statement_id=statements[0], value=1)
        )
        session.add(
            Vote(participant_id=participant.id, statement_id=statements[1], value=-1)
        )
        await session.commit()

        conversation = await session.get(Conversation, conversation_id)
        extracted = await pipeline.extract_votes(session, conversation)

    by_statement = {row["statement_id"]: row["vote"] for row in extracted}
    assert by_statement[statements[0]] == 1     # d'accord reste +1
    assert by_statement[statements[1]] == -1    # pas d'accord reste -1


async def test_a_withdrawn_statement_leaves_the_analysis(
    moderator_client, client, session_factory
) -> None:
    """Une proposition qui sort de la publication sort aussi du calcul, et ses votes
    avec elle.

    Le geste qui l'en sortait était « rejeter » depuis la file de pré-modération ; il a
    disparu au MOD-14 avec la file elle-même. Le geste qui le remplace est le **retrait**
    (MOD-3a), déclenché par un signalement — mais la règle éprouvée ici est la même, et
    elle compte davantage maintenant que tout est publié d'emblée : l'analyse ne doit
    jamais continuer à faire parler un texte qu'on a sorti de la circulation.
    """
    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=2
    )
    proposal = await client.post(
        f"/api/conversations/{slug}/statements", json={"text": "Sera rejetée."}
    )
    proposal_id = proposal.json()["id"]
    await client.post(
        f"/api/conversations/{slug}/votes", json={"statement_id": proposal_id, "value": 1}
    )

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        before = await pipeline.extract_votes(session, conversation)

    await depublier(session_factory, proposal_id, ModerationStatus.retire)

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        after = await pipeline.extract_votes(session, conversation)

    assert proposal_id in {row["statement_id"] for row in before}
    assert proposal_id not in {row["statement_id"] for row in after}


async def test_too_little_data_is_recorded_not_invented(
    moderator_client, session_factory
) -> None:
    """Sur trop peu de votes, PCA + k-means trouverait des groupes dénués de sens.

    Le run est enregistré en `insufficient_data` avec son motif, plutôt que sauté
    en silence : un modérateur doit pouvoir savoir pourquoi rien ne s'affiche.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=2
    )

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        run = await pipeline.analyse(session, conversation)

    assert run.status is AnalysisStatus.insufficient_data
    assert "participant" in run.error_text
    assert run.finished_at is not None


async def test_a_full_run_writes_projections_and_statistics(
    moderator_client, session_factory
) -> None:
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8
    )

    async with session_factory() as session:
        participants = await _populate(session, conversation_id, statements)
        conversation = await session.get(Conversation, conversation_id)
        run = await pipeline.analyse(session, conversation)

        projections = list(
            await session.scalars(
                select(ParticipantProjection).where(
                    ParticipantProjection.run_id == run.id
                )
            )
        )
        stats = list(
            await session.scalars(
                select(StatementStat).where(StatementStat.run_id == run.id)
            )
        )

    assert run.status is AnalysisStatus.ok, run.error_text
    assert run.n_participants == len(participants)
    assert run.n_statements == 8
    assert run.n_votes == len(participants) * 8
    assert run.k is not None and run.k >= 2
    assert len(projections) == len(participants)
    # Une statistique globale par proposition (group_id NULL), plus les
    # propositions représentatives par groupe.
    assert sum(1 for s in stats if s.group_id is None) == 8
    assert any(s.group_id is not None for s in stats)
    assert all(s.priority is not None for s in stats if s.group_id is None)


async def test_the_run_is_reproducible(moderator_client, session_factory) -> None:
    """`random_state` est fixé : deux exécutions sur les mêmes votes doivent donner
    la même partition. Sans cela, les groupes bougeraient à chaque heure."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8
    )

    async with session_factory() as session:
        await _populate(session, conversation_id, statements)
        conversation = await session.get(Conversation, conversation_id)
        first = await pipeline.analyse(session, conversation)
        second = await pipeline.analyse(session, conversation)

        def partition(run_id):
            return sorted(
                (p.participant_id, p.cluster_id)
                for p in run_id
            )

        rows_first = partition(
            await session.scalars(
                select(ParticipantProjection).where(
                    ParticipantProjection.run_id == first.id
                )
            )
        )
        rows_second = partition(
            await session.scalars(
                select(ParticipantProjection).where(
                    ParticipantProjection.run_id == second.id
                )
            )
        )

    assert rows_first == rows_second


async def test_forcing_the_group_count_is_honoured(
    moderator_client, session_factory
) -> None:
    """Parade au constat du chantier B : le k automatique de red-dwarf varie.
    Un modérateur peut le figer par conversation."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8
    )

    async with session_factory() as session:
        await _populate(session, conversation_id, statements)
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2
        await session.commit()
        run = await pipeline.analyse(session, conversation)

    assert run.status is AnalysisStatus.ok, run.error_text
    assert run.k == 2
    assert run.params["force_group_count"] == 2


async def test_only_the_latest_successful_run_is_served(
    moderator_client, session_factory
) -> None:
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8
    )

    async with session_factory() as session:
        await _populate(session, conversation_id, statements)
        conversation = await session.get(Conversation, conversation_id)
        await pipeline.analyse(session, conversation)
        latest = await pipeline.analyse(session, conversation)
        served = await pipeline.latest_ok_run(session, conversation)

    assert served.id == latest.id


async def test_the_worker_only_recomputes_what_moved(
    moderator_client, session_factory
) -> None:
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8
    )

    async with session_factory() as session:
        # Sans aucun vote : rien à recalculer.
        assert await pipeline.conversations_needing_analysis(session) == []

        await _populate(session, conversation_id, statements)
        stale = await pipeline.conversations_needing_analysis(session)
        assert [c.id for c in stale] == [conversation_id]

        conversation = await session.get(Conversation, conversation_id)
        await pipeline.analyse(session, conversation)

        # Après calcul, plus rien ne bouge tant qu'aucun vote n'arrive.
        assert await pipeline.conversations_needing_analysis(session) == []


async def test_a_draft_conversation_is_never_analysed(
    moderator_client, session_factory
) -> None:
    from tests.conftest import create_conversation

    conversation_id = await create_conversation(moderator_client, title="Brouillon")

    async with session_factory() as session:
        statement = Statement(
            conversation_id=conversation_id, text="X", moderation_status="approved"
        )
        session.add(statement)
        await session.flush()
        participant = Participant(anon_token="jeton-brouillon")
        session.add(participant)
        await session.flush()
        session.add(
            Vote(participant_id=participant.id, statement_id=statement.id, value=1)
        )
        await session.commit()

        assert await pipeline.conversations_needing_analysis(session) == []


async def test_the_minimum_vote_threshold_matches_reddwarf(session_factory) -> None:
    """red-dwarf écarte les participants ayant moins de 7 votes (son défaut).

    On l'expose au lieu de le subir : le seuil de configuration doit coïncider. C'est
    désormais le PLAFOND de la borne calculée par conversation
    (app/analysis/thresholds.py), pas la valeur appliquée telle quelle."""
    from reddwarf.implementations.base import run_pipeline
    import inspect

    default = inspect.signature(run_pipeline).parameters["min_user_vote_threshold"].default
    assert settings.analysis_min_user_votes == default


# --- seuil borné par la taille de la conversation ---------------------------------


def test_the_threshold_is_bounded_by_the_number_of_statements() -> None:
    """Le défaut de red-dwarf est un blocage structurel sur une petite conversation.

    Avec 3 propositions, exiger 7 votes rend l'analyse *mathématiquement impossible* :
    aucun participant ne peut atteindre le seuil, quel que soit leur nombre. La borne
    suit la taille de la conversation, sans jamais dépasser le défaut de red-dwarf.
    """
    from app.analysis.thresholds import bound_for

    assert bound_for(1) == 2   # plancher : un vote unique ne « situe » personne
    assert bound_for(3) == 2
    assert bound_for(5) == 3
    assert bound_for(8) == 5
    assert bound_for(11) == 7  # plafond atteint
    assert bound_for(40) == 7  # et jamais dépassé
    assert bound_for(8) <= settings.analysis_min_user_votes


async def test_a_short_conversation_can_still_be_analysed(
    moderator_client, session_factory
) -> None:
    """Le cas de `les-ecoles-du-quartier` : 3 propositions, aucune analyse possible."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Trois propositions"
    )

    async with session_factory() as session:
        await _populate(session, conversation_id, statements, n_participants=8)
        conversation = await session.get(Conversation, conversation_id)
        run = await pipeline.analyse(session, conversation)

    assert run.params["min_user_vote_threshold"] == 2
    # Les 8 participants entrent dans la matrice ; avec le seuil de 7 il y en aurait 0.
    assert run.n_participants == 8
    assert run.status is AnalysisStatus.ok, run.error_text


async def test_a_moderator_can_override_the_threshold(
    moderator_client, session_factory
) -> None:
    """L'override l'emporte sur la borne — et un seuil trop haut se voit."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Seuil impose"
    )

    async with session_factory() as session:
        await _populate(session, conversation_id, statements, n_participants=8)
        conversation = await session.get(Conversation, conversation_id)
        conversation.min_user_votes = 7
        await session.commit()
        run = await pipeline.analyse(session, conversation)

    assert run.params["min_user_vote_threshold"] == 7
    assert run.n_participants == 0
    assert run.status is AnalysisStatus.insufficient_data
    assert "7 votes" in run.error_text


async def test_changing_the_threshold_schedules_a_recomputation(
    moderator_client, session_factory
) -> None:
    """Comme force_group_count : le réglage ne doit pas attendre un nouveau vote.

    Le seuil bouge aussi tout seul — approuver une proposition change la borne sans
    créer le moindre vote, et l'analyse doit en tenir compte.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8
    )

    async with session_factory() as session:
        await _populate(session, conversation_id, statements)
        conversation = await session.get(Conversation, conversation_id)
        await pipeline.analyse(session, conversation)
        assert await pipeline.conversations_needing_analysis(session) == []

        conversation.min_user_votes = 4
        await session.commit()
        stale = await pipeline.conversations_needing_analysis(session)

    assert [c.id for c in stale] == [conversation_id]


# --- purge des calculs dépassés (chantier G9) --------------------------------------


async def test_the_purge_frees_old_runs_but_never_the_last_good_one(
    moderator_client, session_factory
) -> None:
    """Ce qui est rendu, et ce qui ne l'est jamais.

    Au quart d'heure, `participant_projection` gagne quatre fois plus de lignes par
    heure : sans borne, 300 débats de 300 participants en écrivent 8,6 millions par
    jour. Trois propriétés doivent tenir ensemble, et c'est leur conjonction qui est
    éprouvée ici :

    1. les grosses tables d'un calcul dépassé et ancien sont rendues ;
    2. le DERNIER calcul abouti garde les siennes **quel que soit son âge** — c'est lui
       que la carte, la mini-barre, le routage et l'annonce de groupe lisent tous, et
       une conversation qu'on ne vote plus depuis un mois perdrait ses groupes ;
    3. la ligne `analysis_run` survit dans tous les cas : c'est la trace qu'on relit
       quand un résultat est contesté, et elle ne pèse que quelques octets.
    """
    from datetime import datetime, timedelta, timezone

    from app.models import AnalysisRun

    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8, title="Purge"
    )

    async with session_factory() as session:
        await _populate(session, conversation_id, statements)
        conversation = await session.get(Conversation, conversation_id)
        vieux = await pipeline.analyse(session, conversation)
        recent = await pipeline.analyse(session, conversation)
        assert vieux.status is AnalysisStatus.ok and recent.status is AnalysisStatus.ok

        # Les deux calculs sont vieillis bien au-delà de la rétention : le récent n'est
        # protégé que parce qu'il est le dernier abouti, jamais par son âge.
        ancien = datetime.now(timezone.utc) - timedelta(
            hours=settings.analysis_history_retention_hours + 24
        )
        vieux.finished_at = ancien - timedelta(hours=1)
        recent.finished_at = ancien
        await session.commit()

        avant = await session.scalar(
            select(func.count(ParticipantProjection.id)).where(
                ParticipantProjection.run_id == vieux.id
            )
        )
        assert avant > 0, "le calcul dépassé n'a rien écrit : le test ne prouve rien"

        otees = await pipeline.purge_calculs_perimes(session)

        restant_vieux = await session.scalar(
            select(func.count(ParticipantProjection.id)).where(
                ParticipantProjection.run_id == vieux.id
            )
        )
        restant_recent = await session.scalar(
            select(func.count(ParticipantProjection.id)).where(
                ParticipantProjection.run_id == recent.id
            )
        )
        stats_vieux = await session.scalar(
            select(func.count(StatementStat.id)).where(
                StatementStat.run_id == vieux.id
            )
        )
        calculs = await session.scalar(
            select(func.count(AnalysisRun.id)).where(
                AnalysisRun.conversation_id == conversation_id
            )
        )

    assert otees > 0
    assert restant_vieux == 0, "le calcul dépassé garde ses projections"
    assert restant_recent > 0, "le dernier calcul abouti a été purgé : le site est aveugle"
    assert stats_vieux == 0
    assert calculs == 2, "la trace du calcul a été effacée avec ses projections"


async def test_a_recent_run_is_left_alone_even_once_superseded(
    moderator_client, session_factory
) -> None:
    """La rétention laisse de quoi enquêter sur un calcul douteux.

    Un calcul dépassé depuis dix minutes n'est pas un déchet : c'est celui qu'on relit
    quand quelqu'un signale que son groupe a bougé sans raison.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8, title="Retention"
    )

    async with session_factory() as session:
        await _populate(session, conversation_id, statements)
        conversation = await session.get(Conversation, conversation_id)
        depasse = await pipeline.analyse(session, conversation)
        await pipeline.analyse(session, conversation)

        otees = await pipeline.purge_calculs_perimes(session)

        restant = await session.scalar(
            select(func.count(ParticipantProjection.id)).where(
                ParticipantProjection.run_id == depasse.id
            )
        )

    assert otees == 0
    assert restant > 0
