"""Enregistrement des votes : convention de signe, upsert au re-vote, refus."""

from sqlalchemy import func, select

from app.models import ModerationStatus, Statement, Vote, VoteValue
from tests.conftest import login, open_conversation, register


async def test_the_three_values_are_recorded_with_the_reddwarf_convention(
    moderator_client, client, session_factory
) -> None:
    """agree = +1, disagree = -1, pass = 0 — l'inverse de Pol.is, volontairement."""
    _, slug, statements = await open_conversation(moderator_client, statements=3)

    for statement_id, value in zip(statements, (1, -1, 0)):
        response = await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement_id, "value": value},
        )
        assert response.status_code == 200, response.text

    async with session_factory() as session:
        stored = {
            vote.statement_id: vote.value
            for vote in await session.scalars(select(Vote))
        }

    assert stored == dict(zip(statements, (1, -1, 0)))
    assert VoteValue.agree == 1 and VoteValue.disagree == -1 and VoteValue.pass_ == 0


async def test_a_revote_updates_the_row_instead_of_adding_one(
    moderator_client, client, session_factory
) -> None:
    _, slug, statements = await open_conversation(moderator_client, statements=2)
    target = statements[0]

    first = await client.post(
        f"/api/conversations/{slug}/votes", json={"statement_id": target, "value": 1}
    )
    second = await client.post(
        f"/api/conversations/{slug}/votes", json={"statement_id": target, "value": -1}
    )

    assert first.json()["created"] is True
    assert second.json()["created"] is False  # mise à jour, pas insertion

    async with session_factory() as session:
        rows = await session.scalar(
            select(func.count(Vote.id)).where(Vote.statement_id == target)
        )
        value = await session.scalar(
            select(Vote.value).where(Vote.statement_id == target)
        )

    assert rows == 1
    assert value == -1


async def test_an_invalid_value_is_refused(moderator_client, client) -> None:
    _, slug, statements = await open_conversation(moderator_client, statements=1)

    response = await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": statements[0], "value": 2},
    )

    assert response.status_code == 422


async def test_voting_on_a_statement_from_another_conversation_is_refused(
    moderator_client, client
) -> None:
    _, slug_a, _ = await open_conversation(moderator_client, title="A", statements=1)
    _, _, statements_b = await open_conversation(
        moderator_client, title="B", statements=1
    )

    response = await client.post(
        f"/api/conversations/{slug_a}/votes",
        json={"statement_id": statements_b[0], "value": 1},
    )

    assert response.status_code == 404


async def test_voting_on_a_statement_that_is_not_approved_is_refused(
    moderator_client, client, session_factory
) -> None:
    """Et avec le même 404 qu'une proposition inexistante : répondre autre chose
    révélerait l'existence d'une proposition qui n'est pas publique.

    Depuis le MOD-14 une proposition déposée naît `approved` : l'état non publié ne
    s'obtient plus par le dépôt, mais il existe toujours — un retrait conservatoire, une
    amorce dont la conversation attend. La règle, elle, n'a pas bougé, et c'est elle que
    ce test tient : **seul `approved` se vote.**
    """
    _, slug, _ = await open_conversation(moderator_client, statements=1)
    proposal = await client.post(
        f"/api/conversations/{slug}/statements", json={"text": "Publiée puis retirée."}
    )
    async with session_factory() as session:
        statement = await session.get(Statement, proposal.json()["id"])
        statement.moderation_status = ModerationStatus.retire
        await session.commit()

    response = await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": proposal.json()["id"], "value": 1},
    )

    assert response.status_code == 404


async def test_a_closed_conversation_refuses_votes(moderator_client, client) -> None:
    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=1
    )
    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}",
        data={
            "title": "Conversation ouverte",
            "description": "",
            "state": "closed",
            "themes": ["institutions"],
            "moderation_mode": "pre",
        },
        follow_redirects=False,
    )

    response = await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": statements[0], "value": 1},
    )

    assert response.status_code == 409


# --- qui peut voter --------------------------------------------------------------


async def test_an_anonymous_visitor_can_vote(moderator_client, client) -> None:
    _, slug, statements = await open_conversation(moderator_client, statements=1)

    response = await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": statements[0], "value": 1},
    )

    assert response.status_code == 200
    assert response.json()["participant_id"] > 0


async def test_an_UNVERIFIED_account_can_vote(moderator_client, client) -> None:
    """La promesse tenue depuis C1, enfin vérifiée sur un vote réel."""
    _, slug, statements = await open_conversation(moderator_client, statements=1)
    await register(client, "nadia@exemple.fr")
    await login(client, "nadia@exemple.fr")
    assert (await client.get("/api/me")).json()["account"]["is_verified"] is False

    response = await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": statements[0], "value": 1},
    )

    assert response.status_code == 200


# --- proposition suivante --------------------------------------------------------


async def test_next_statement_walks_through_without_repeating(
    moderator_client, client
) -> None:
    _, slug, statements = await open_conversation(moderator_client, statements=4)

    seen = []
    while True:
        step = (await client.get(f"/api/conversations/{slug}/next-statement")).json()
        if step["statement"] is None:
            break
        seen.append(step["statement"]["id"])
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": step["statement"]["id"], "value": 0},
        )

    assert sorted(seen) == sorted(statements)
    assert len(seen) == len(set(seen))  # jamais deux fois la même


async def test_remaining_counts_down(moderator_client, client) -> None:
    _, slug, statements = await open_conversation(moderator_client, statements=3)

    before = (await client.get(f"/api/conversations/{slug}/next-statement")).json()
    vote = await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": statements[0], "value": 1},
    )
    after = (await client.get(f"/api/conversations/{slug}/next-statement")).json()

    assert before["remaining"] == 3
    assert vote.json()["remaining"] == 2
    assert after["remaining"] == 2


async def test_a_statement_that_is_not_approved_is_never_proposed_for_voting(
    moderator_client, client, session_factory
) -> None:
    """Même règle, vue du tirage : ce qui n'est pas publié n'est ni servi ni compté."""
    _, slug, statements = await open_conversation(moderator_client, statements=1)
    depot = await client.post(
        f"/api/conversations/{slug}/statements", json={"text": "Publiée puis retirée."}
    )
    async with session_factory() as session:
        statement = await session.get(Statement, depot.json()["id"])
        statement.moderation_status = ModerationStatus.retire
        await session.commit()

    step = (await client.get(f"/api/conversations/{slug}/next-statement")).json()

    assert step["remaining"] == 1  # la proposition retirée n'est pas comptée
    assert step["statement"]["id"] == statements[0]


# --- l'annonce du groupe en fin de parcours ----------------------------------------


async def test_the_group_is_announced_only_when_the_journey_ends(
    moderator_client, client
) -> None:
    """Le groupe voyage dans la réponse qui clôt le parcours, et dans elle seule.

    Le G5 avait tranché qu'on ne fait pas attendre l'écran de fin pour un appel de
    plus ; le groupe emprunte donc la réponse déjà attendue. La contrepartie est qu'il
    ne doit rien coûter pendant le vote : tant qu'il reste une proposition à servir, le
    champ est vide et aucune requête de groupe n'est faite.
    """
    _, slug, statements = await open_conversation(moderator_client, statements=3)

    for reste, statement_id in enumerate(statements):
        avant = (await client.get(f"/api/conversations/{slug}/next-statement")).json()
        assert avant["statement"] is not None
        assert avant["groupe"] is None, "le groupe ne se lit pas pendant le vote"
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": avant["statement"]["id"], "value": 1},
        )

    fin = (await client.get(f"/api/conversations/{slug}/next-statement")).json()

    assert fin["statement"] is None
    assert fin["groupe"] is not None


async def test_someone_who_just_voted_is_told_why_they_have_no_group_yet(
    moderator_client, client
) -> None:
    """Jamais un silence, et jamais un groupe inventé.

    Quelqu'un qui vient de voter pour la première fois n'est dans aucun calcul : sa
    place n'existe pas encore, et la base de projection n'est pas persistée — on ne
    peut donc pas la deviner sans risquer de contredire la carte une heure plus tard.
    Ce qui est dû à cette personne, c'est le motif.
    """
    _, slug, statements = await open_conversation(moderator_client, statements=2)
    for statement_id in statements:
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement_id, "value": 1},
        )

    annonce = (await client.get(f"/api/conversations/{slug}/next-statement")).json()[
        "groupe"
    ]

    assert annonce["nom"] is None
    assert annonce["raison"], "un groupe inconnu doit être expliqué, pas tu"


async def test_a_placed_participant_hears_their_group_name(
    moderator_client, client, session_factory
) -> None:
    """Et quand le calcul situe la personne, le nom est celui de la carte.

    Le même que le bloc « votre groupe » en haut de la page : une seule source
    (`groups.for_participant`), donc pas de version qui dérive.
    """
    from app.analysis import pipeline
    from app.models import Conversation, Participant
    from app.services import groups as groups_service
    from tests.test_carte import _peupler

    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8
    )
    # Ce visiteur-ci vote sur tout, puis le calcul tourne : il est donc situé.
    for statement_id in statements:
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement_id, "value": 1 if statement_id % 2 else -1},
        )
    async with session_factory() as session:
        await _peupler(session, statements, prefixe="annonce")
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2
        await session.commit()
        await pipeline.analyse(session, conversation)

    fin = (await client.get(f"/api/conversations/{slug}/next-statement")).json()
    annonce = fin["groupe"]

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        # Le participant du client, désigné par la réponse elle-même : `_peupler` en
        # crée d'autres après lui, donc « le dernier créé » ne serait pas nous.
        moi = await session.get(Participant, fin["participant_id"])
        attendu = await groups_service.for_participant(session, conversation, moi)

    if annonce["nom"] is not None:
        assert annonce["raison"] is None
        assert annonce["nom"] == attendu.name
    else:
        # Le calcul peut ne pas le situer (trop peu de votes au moment du run) : c'est
        # un état normal, et il doit alors porter son motif.
        assert annonce["raison"]


# --- le temps restant avant le prochain calcul (chantier G9) -----------------------


async def test_the_waiting_person_is_told_when_the_next_run_takes_place(
    moderator_client, client, session_factory
) -> None:
    """À quelqu'un qui a fait sa part, on chiffre l'attente plutôt que la fréquence.

    Le cas visé : la personne a voté sur tout, un calcul a déjà abouti sur cette
    consultation, mais il est antérieur à ses votes — elle n'y figure donc pas. Il ne
    lui reste rien à faire qu'attendre, et c'est cette attente-là qu'on lui donne.

    La réponse porte une DATE et non une durée : la page reste ouverte après le dernier
    vote, et « dans 12 minutes » figé au moment de la réponse deviendrait faux au bout
    de treize. Le navigateur en tire le délai, et le rafraîchit.
    """
    from datetime import datetime, timezone

    from app.analysis import pipeline
    from app.config import settings
    from app.models import Conversation
    from tests.test_carte import _peupler

    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8, title="Attente"
    )
    # Le calcul tourne AVANT que ce visiteur ne vote : il est donc absent du dernier
    # calcul abouti, ce qui est exactement l'état d'un nouveau venu.
    async with session_factory() as session:
        await _peupler(session, statements, prefixe="attente")
        conversation = await session.get(Conversation, conversation_id)
        await pipeline.analyse(session, conversation)

    for statement_id in statements:
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement_id, "value": 1 if statement_id % 2 else -1},
        )

    annonce = (await client.get(f"/api/conversations/{slug}/next-statement")).json()[
        "groupe"
    ]

    assert annonce["nom"] is None, "le calcul est antérieur à ces votes"
    assert annonce["prochain_calcul"], "l'attente n'est pas chiffrée"
    prochain = datetime.fromisoformat(annonce["prochain_calcul"])
    reste = (prochain - datetime.now(timezone.utc)).total_seconds()
    assert 0 < reste <= settings.analysis_interval_seconds
    # Calé sur la grille absolue : c'est ce qui rend l'annonce tenable.
    assert int(prochain.timestamp()) % settings.analysis_interval_seconds == 0


async def test_nobody_is_given_a_countdown_they_cannot_use(
    moderator_client, client, session_factory
) -> None:
    """Deux situations où le compte à rebours serait du bruit, ou pire un contresens.

    Quelqu'un qui connaît son groupe n'a rien à attendre. Et quelqu'un à qui il manque
    des votes n'est pas débloqué par le temps : attendre ne le situera pas, c'est voter
    qui le fera — un délai affiché à côté de « il vous faut 5 votes » laisserait croire
    le contraire.
    """
    from app.analysis import pipeline
    from app.models import Conversation, Participant
    from app.services import groups as groups_service
    from tests.test_carte import _peupler

    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8, title="Sans rebours"
    )
    for statement_id in statements:
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement_id, "value": 1 if statement_id % 2 else -1},
        )
    async with session_factory() as session:
        await _peupler(session, statements, prefixe="rebours")
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2
        await session.commit()
        await pipeline.analyse(session, conversation)

    fin = (await client.get(f"/api/conversations/{slug}/next-statement")).json()
    annonce = fin["groupe"]
    assert annonce["nom"], "le calcul devait situer ce votant"
    assert annonce["prochain_calcul"] is None, "il sait déjà : rien à attendre"

    # Le second cas se lit sur le service, et non par l'API : à la fin d'un parcours,
    # on a par construction voté sur toutes les propositions, donc toujours atteint le
    # seuil. C'est `attend_le_calcul` qui porte le partage, et c'est lui qui décide.
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        nouveau = Participant(anon_token="rebours-timide")
        session.add(nouveau)
        await session.flush()
        vue = await groups_service.for_participant(session, conversation, nouveau)

    assert vue.name is None
    assert vue.attend_le_calcul is False, "voter, et non attendre, est ce qui le situera"
    assert "votes pour être situé" in vue.reason
