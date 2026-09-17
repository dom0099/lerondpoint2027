"""Fusion des votes quand un participant anonyme dormant rejoint un compte.

Ce cas avait été identifié à C1 (journal, commit 54374ac) : la ligne du participant
anonyme n'était pas supprimée mais laissée dormante, si bien que ses votes auraient
compté comme un participant distinct dans l'analyse — exactement le double comptage
que la contrainte UNIQUE sur `user_id` sert à empêcher.

Règle retenue : **le vote du compte l'emporte** en cas de conflit, les votes propres à
l'anonyme sont récupérés, la ligne anonyme disparaît, et la déconnexion réémet un
jeton neuf.
"""

from sqlalchemy import select

from app.models import Participant, Statement, Vote
from tests.conftest import login, open_conversation, register


async def test_merge_moves_votes_and_the_account_wins_conflicts(
    moderator_client, client_factory, session_factory
) -> None:
    _, slug, statements = await open_conversation(moderator_client, statements=3)
    first, second, third = statements

    # Navigateur A : le compte existe et a déjà voté sur `first`.
    async with client_factory() as browser_a:
        await register(browser_a, "marie@exemple.fr")
        await login(browser_a, "marie@exemple.fr")
        account_participant = (await browser_a.get("/api/me")).json()["participant_id"]
        await browser_a.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": first, "value": 1},
        )

    # Navigateur B : anonyme, vote sur la MÊME proposition (en sens inverse) et sur
    # une autre, puis dépose une proposition.
    async with client_factory() as browser_b:
        anonymous_participant = (await browser_b.get("/api/me")).json()["participant_id"]
        assert anonymous_participant != account_participant

        await browser_b.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": first, "value": -1},   # conflit
        )
        await browser_b.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": second, "value": -1},  # propre à l'anonyme
        )
        await browser_b.post(
            f"/api/conversations/{slug}/statements", json={"text": "Proposée en anonyme."}
        )

        # Connexion depuis ce second navigateur -> fusion.
        await login(browser_b, "marie@exemple.fr")
        after_login = (await browser_b.get("/api/me")).json()

    assert after_login["participant_id"] == account_participant

    async with session_factory() as session:
        votes = {
            vote.statement_id: vote.value
            for vote in await session.scalars(
                select(Vote).where(Vote.participant_id == account_participant)
            )
        }
        orphan = await session.get(Participant, anonymous_participant)
        proposal = await session.scalar(
            select(Statement).where(Statement.text == "Proposée en anonyme.")
        )

    # Le vote du compte l'emporte sur la proposition disputée…
    assert votes[first] == 1
    # …et le vote que seul l'anonyme avait émis est récupéré.
    assert votes[second] == -1
    assert third not in votes
    # La ligne anonyme a disparu : plus de double comptage possible dans l'analyse.
    assert orphan is None
    # La proposition déposée suit la personne.
    assert proposal.author_participant_id == account_participant


async def test_no_vote_is_lost_when_there_is_no_conflict(
    moderator_client, client_factory, session_factory
) -> None:
    _, slug, statements = await open_conversation(moderator_client, statements=3)

    async with client_factory() as browser_a:
        await register(browser_a, "paul@exemple.fr")
        await login(browser_a, "paul@exemple.fr")
        account_participant = (await browser_a.get("/api/me")).json()["participant_id"]

    async with client_factory() as browser_b:
        for statement_id in statements:
            await browser_b.post(
                f"/api/conversations/{slug}/votes",
                json={"statement_id": statement_id, "value": 1},
            )
        await login(browser_b, "paul@exemple.fr")

    async with session_factory() as session:
        moved = await session.scalars(
            select(Vote).where(Vote.participant_id == account_participant)
        )

    assert sorted(vote.statement_id for vote in moved) == sorted(statements)


async def test_logging_out_issues_a_brand_new_anonymous_token(
    moderator_client, client_factory, session_factory
) -> None:
    """Après fusion, l'ancien jeton est caduc : la déconnexion doit en donner un neuf.

    Sans cela, le navigateur repartirait vers une ligne supprimée.
    """
    _, slug, statements = await open_conversation(moderator_client, statements=2)

    async with client_factory() as browser_a:
        await register(browser_a, "sonia@exemple.fr")
        await login(browser_a, "sonia@exemple.fr")
        account_participant = (await browser_a.get("/api/me")).json()["participant_id"]

    async with client_factory() as browser_b:
        anonymous_participant = (await browser_b.get("/api/me")).json()["participant_id"]
        await browser_b.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statements[0], "value": 1},
        )
        await login(browser_b, "sonia@exemple.fr")
        assert (await browser_b.get("/api/me")).json()["participant_id"] == account_participant

        await browser_b.post("/auth/logout")
        after_logout = (await browser_b.get("/api/me")).json()

    assert after_logout["authenticated"] is False
    # Ni l'ancien anonyme (supprimé), ni celui du compte (qui ne doit pas fuiter).
    assert after_logout["participant_id"] not in {
        anonymous_participant,
        account_participant,
    }

    async with session_factory() as session:
        fresh = await session.get(Participant, after_logout["participant_id"])
    assert fresh is not None
    assert fresh.user_id is None
    assert fresh.anon_token is not None


async def test_a_first_time_account_keeps_its_anonymous_participant(
    moderator_client, client
) -> None:
    """Contre-épreuve : sans participant préexistant côté compte, il n'y a pas de
    fusion mais un simple rattachement — la même ligne, donc rien à déplacer."""
    _, slug, statements = await open_conversation(moderator_client, statements=2)

    anonymous_participant = (await client.get("/api/me")).json()["participant_id"]
    await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": statements[0], "value": 1},
    )
    await register(client, "hugo2@exemple.fr")
    await login(client, "hugo2@exemple.fr")

    assert (await client.get("/api/me")).json()["participant_id"] == anonymous_participant
