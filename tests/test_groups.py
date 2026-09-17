"""Stabilité des groupes dans le temps, et affichage aux participants."""

from sqlalchemy import select, update

from app.analysis import pipeline
from app.models import (
    AnalysisRun,
    AnalysisStatus,
    Conversation,
    Participant,
    ParticipantProjection,
    Vote,
)
from tests.conftest import login, open_conversation, register


async def _peupler(session, statements, effectif=10):
    """Deux camps francs : les pairs contre les impairs."""
    participants = []
    for index in range(effectif):
        participant = Participant(anon_token=f"jeton-groupe-{index}")
        session.add(participant)
        participants.append(participant)
    await session.flush()

    for index, participant in enumerate(participants):
        camp = index % 2
        for position, statement_id in enumerate(statements):
            session.add(
                Vote(
                    participant_id=participant.id,
                    statement_id=statement_id,
                    value=1 if (position % 2 == camp) else -1,
                )
            )
    await session.commit()
    return participants


async def _changer_de_camp(session, participant, statements, camp):
    """Fait basculer quelqu'un dans l'autre camp, vote par vote."""
    for position, statement_id in enumerate(statements):
        await session.execute(
            update(Vote)
            .where(
                Vote.participant_id == participant.id,
                Vote.statement_id == statement_id,
            )
            .values(value=1 if (position % 2 == camp) else -1)
        )
    await session.commit()


async def _composition(session, run_id):
    """identité stable -> participants, et étiquette brute -> participants."""
    lignes = list(
        await session.execute(
            select(
                ParticipantProjection.participant_id,
                ParticipantProjection.cluster_id,
                ParticipantProjection.stable_group_id,
            ).where(ParticipantProjection.run_id == run_id)
        )
    )
    stables: dict[int, set[int]] = {}
    bruts: dict[int, set[int]] = {}
    for participant_id, brut, stable in lignes:
        stables.setdefault(stable, set()).add(participant_id)
        bruts.setdefault(brut, set()).add(participant_id)
    return stables, bruts


async def test_identities_survive_three_recomputations_with_real_vote_changes(
    moderator_client, session_factory
) -> None:
    """Trois calculs successifs, avec de vrais changements de votes entre chacun.

    Ce qui doit tenir : deux personnes qui ne changent jamais d'avis gardent la
    même identité de groupe du premier au dernier calcul, quoi que fasse k-means
    avec ses étiquettes.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8
    )

    async with session_factory() as session:
        participants = await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2  # isole l'appariement de la variation de k
        await session.commit()

        constant_pair = participants[0]    # camp 0, ne bouge jamais
        constant_impair = participants[1]  # camp 1, ne bouge jamais

        # --- calcul 1 -------------------------------------------------------------
        premier = await pipeline.analyse(session, conversation)
        assert premier.status is AnalysisStatus.ok, premier.error_text
        stables_1, _ = await _composition(session, premier.id)
        groupe_pair_1 = await session.scalar(
            select(ParticipantProjection.stable_group_id).where(
                ParticipantProjection.run_id == premier.id,
                ParticipantProjection.participant_id == constant_pair.id,
            )
        )
        groupe_impair_1 = await session.scalar(
            select(ParticipantProjection.stable_group_id).where(
                ParticipantProjection.run_id == premier.id,
                ParticipantProjection.participant_id == constant_impair.id,
            )
        )
        assert groupe_pair_1 != groupe_impair_1

        # --- changement de votes, puis calcul 2 -----------------------------------
        await _changer_de_camp(session, participants[8], statements, camp=1)
        deuxieme = await pipeline.analyse(session, conversation)
        assert deuxieme.status is AnalysisStatus.ok

        # --- autre changement, puis calcul 3 --------------------------------------
        await _changer_de_camp(session, participants[9], statements, camp=0)
        troisieme = await pipeline.analyse(session, conversation)
        assert troisieme.status is AnalysisStatus.ok

        identites = {}
        for nom, run in (("1", premier), ("2", deuxieme), ("3", troisieme)):
            identites[nom] = {
                "pair": await session.scalar(
                    select(ParticipantProjection.stable_group_id).where(
                        ParticipantProjection.run_id == run.id,
                        ParticipantProjection.participant_id == constant_pair.id,
                    )
                ),
                "impair": await session.scalar(
                    select(ParticipantProjection.stable_group_id).where(
                        ParticipantProjection.run_id == run.id,
                        ParticipantProjection.participant_id == constant_impair.id,
                    )
                ),
            }

    # L'identité tient sur les trois calculs, malgré deux bascules de participants.
    assert identites["1"]["pair"] == identites["2"]["pair"] == identites["3"]["pair"]
    assert (
        identites["1"]["impair"]
        == identites["2"]["impair"]
        == identites["3"]["impair"]
    )
    assert identites["3"]["pair"] != identites["3"]["impair"]


async def test_a_participant_who_switches_camp_changes_group_but_not_the_groups(
    moderator_client, session_factory
) -> None:
    """Le contraire du test précédent : ce qui DOIT bouger bouge bien."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8
    )

    async with session_factory() as session:
        participants = await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2
        await session.commit()

        premier = await pipeline.analyse(session, conversation)
        bascule = participants[2]
        avant = await session.scalar(
            select(ParticipantProjection.stable_group_id).where(
                ParticipantProjection.run_id == premier.id,
                ParticipantProjection.participant_id == bascule.id,
            )
        )

        await _changer_de_camp(session, bascule, statements, camp=1)
        second = await pipeline.analyse(session, conversation)
        apres = await session.scalar(
            select(ParticipantProjection.stable_group_id).where(
                ParticipantProjection.run_id == second.id,
                ParticipantProjection.participant_id == bascule.id,
            )
        )
        # Les autres, eux, n'ont pas bougé.
        temoin_avant = await session.scalar(
            select(ParticipantProjection.stable_group_id).where(
                ParticipantProjection.run_id == premier.id,
                ParticipantProjection.participant_id == participants[0].id,
            )
        )
        temoin_apres = await session.scalar(
            select(ParticipantProjection.stable_group_id).where(
                ParticipantProjection.run_id == second.id,
                ParticipantProjection.participant_id == participants[0].id,
            )
        )

    assert avant != apres        # la personne a changé de groupe
    assert temoin_avant == temoin_apres  # le groupe, lui, garde son identité


async def test_the_mapping_is_recorded_on_the_run(
    moderator_client, session_factory
) -> None:
    """Pour pouvoir expliquer après coup pourquoi un groupe a gardé son identité."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        run = await pipeline.analyse(session, conversation)

    assert run.group_mapping
    assert all(valeur is not None for valeur in run.group_mapping.values())


# --- affichage aux participants ----------------------------------------------------


async def test_nothing_is_shown_before_the_first_computation(
    moderator_client, client
) -> None:
    _, slug, _ = await open_conversation(moderator_client)

    page = await client.get(f"/c/{slug}")
    api = await client.get(f"/api/conversations/{slug}/my-group")

    assert "groupe" not in page.text.lower() or "Vous êtes dans le" not in page.text
    assert api.json() is None


async def test_a_participant_sees_their_group_and_its_size(
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

    # Ce visiteur vote comme le camp des pairs.
    detail = (await client.get(f"/api/conversations/{slug}")).json()
    for position, statement in enumerate(detail["statements"]):
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement["id"], "value": 1 if position % 2 == 0 else -1},
        )

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        await pipeline.analyse(session, conversation)

    groupe = (await client.get(f"/api/conversations/{slug}/my-group")).json()
    page = await client.get(f"/c/{slug}")

    assert groupe["name"] in {"A", "B"}
    assert groupe["others"] >= 1
    assert groupe["total_participants"] == 11
    assert groupe["group_count"] == 2
    assert f"groupe {groupe['name']}" in page.text
    assert f"avec {groupe['others']} autre" in page.text


async def test_someone_with_too_few_votes_is_told_why(
    moderator_client, client, session_factory
) -> None:
    """Jamais un silence : on explique pourquoi la personne n'est pas située."""
    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        await pipeline.analyse(session, conversation)

    detail = (await client.get(f"/api/conversations/{slug}")).json()
    await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": detail["statements"][0]["id"], "value": 1},
    )

    groupe = (await client.get(f"/api/conversations/{slug}/my-group")).json()
    page = await client.get(f"/c/{slug}")

    assert groupe["name"] is None
    # Le seuil annoncé est celui de CETTE conversation : 8 propositions approuvées
    # -> ceil(8 x 0,6) = 5 votes, et non les 7 de red-dwarf (app/analysis/thresholds).
    assert "5 votes" in groupe["reason"]
    assert "vous en avez émis 1" in groupe["reason"]
    assert "5 votes" in page.text


# --- réglage du nombre de groupes par le modérateur --------------------------------


async def test_a_moderator_can_freeze_the_group_count_from_the_editor(
    moderator_client, session_factory
) -> None:
    conversation_id, _, _ = await open_conversation(moderator_client, title="Réglage")

    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}",
        data={
            "title": "Réglage",
            "description": "",
            "state": "open",
            "themes": ["institutions"],
            "moderation_mode": "pre",
            "allow_participant_statements": "1",
            "force_group_count": "3",
        },
        follow_redirects=False,
    )

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
    assert conversation.force_group_count == 3


async def test_going_back_to_automatic_clears_the_setting(
    moderator_client, session_factory
) -> None:
    conversation_id, _, _ = await open_conversation(moderator_client, title="Réglage")
    donnees = {
        "title": "Réglage",
        "description": "",
        "state": "open",
        "themes": ["institutions"],
        "moderation_mode": "pre",
        "allow_participant_statements": "1",
    }
    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}",
        data={**donnees, "force_group_count": "4"},
        follow_redirects=False,
    )
    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}",
        data={**donnees, "force_group_count": ""},
        follow_redirects=False,
    )

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
    assert conversation.force_group_count is None


async def test_changing_the_setting_schedules_a_recomputation(
    moderator_client, session_factory
) -> None:
    """Sans cela le réglage resterait sans effet visible pendant une heure — voire
    indéfiniment sur une conversation où plus personne ne vote."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8, title="Recalcul"
    )
    async with session_factory() as session:
        await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        await pipeline.analyse(session, conversation)
        # Rien n'a bougé : plus rien à recalculer.
        assert await pipeline.conversations_needing_analysis(session) == []

    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}",
        data={
            "title": "Recalcul",
            "description": "",
            "state": "open",
            "themes": ["institutions"],
            "moderation_mode": "pre",
            "allow_participant_statements": "1",
            "force_group_count": "2",
        },
        follow_redirects=False,
    )

    async with session_factory() as session:
        a_recalculer = await pipeline.conversations_needing_analysis(session)
    assert [c.id for c in a_recalculer] == [conversation_id]


async def test_a_group_of_one_person_is_not_announced(
    moderator_client, client, session_factory
) -> None:
    """Annoncer « groupe C, avec 0 autre personne » désignerait publiquement un
    participant isolé, et ne lui apprendrait rien. Un groupe d'une seule personne est
    un artefact du découpage, pas une opinion partagée.

    L'identité stable est forcée à la main : le test porte sur l'affichage, il ne doit
    pas dépendre du hasard de k-means pour produire un groupe singleton.
    """
    from app.services import groups as groups_service

    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        participants = await _peupler(session, statements)
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2
        await session.commit()
        run = await pipeline.analyse(session, conversation)

        isole = participants[0]
        await session.execute(
            update(ParticipantProjection)
            .where(
                ParticipantProjection.run_id == run.id,
                ParticipantProjection.participant_id == isole.id,
            )
            .values(stable_group_id=99)
        )
        await session.commit()

        vue_isole = await groups_service.for_participant(session, conversation, isole)
        vue_autre = await groups_service.for_participant(
            session, conversation, participants[1]
        )

    assert vue_isole.name is None
    assert "aucun groupe constitué" in vue_isole.reason
    assert vue_isole.others == 0
    # Les autres, eux, voient toujours leur groupe.
    assert vue_autre.name is not None
    assert vue_autre.others >= 1


async def test_the_page_says_nothing_about_a_group_of_one(
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
            json={"statement_id": statement["id"], "value": 1 if position % 3 else -1},
        )

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        run = await pipeline.analyse(session, conversation)
        nouveau = (await client.get("/api/me")).json()["participant_id"]
        await session.execute(
            update(ParticipantProjection)
            .where(
                ParticipantProjection.run_id == run.id,
                ParticipantProjection.participant_id == nouveau,
            )
            .values(stable_group_id=99)
        )
        await session.commit()

    page = await client.get(f"/c/{slug}")
    api = (await client.get(f"/api/conversations/{slug}/my-group")).json()

    assert api["name"] is None
    assert "Vous êtes dans le" not in page.text
    assert "aucun groupe constitué" in page.text
