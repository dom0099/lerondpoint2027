"""Niveaux et badges."""

import pytest
from sqlalchemy import func, select

from app.models import UserBadge
from app.models.gamification import BADGE_CATALOG, FIRST_CONVERSATION, FIRST_VOTE
from app.services import gamification
from tests.conftest import login, open_conversation, register, token_from


# --- la fonction de niveau, pure ---------------------------------------------------


@pytest.mark.parametrize(
    ("votes", "conversations", "attendu"),
    [
        (0, 0, 1),      # rien
        (9, 0, 1),      # juste sous le palier
        (10, 0, 2),     # pile au palier
        (29, 0, 2),
        (30, 0, 3),
        (0, 1, 2),      # une conversation vaut 25 points
        (5, 1, 3),      # 5 + 25 = 30
        (300, 0, 6),
        (10000, 100, 6),  # plafonné au dernier palier
    ],
)
def test_level_thresholds(votes, conversations, attendu) -> None:
    niveau, _, _ = gamification.level_for(votes, conversations)
    assert niveau == attendu


def test_points_combine_votes_and_conversations() -> None:
    assert gamification.points_for(7, 2) == 7 + 50


def test_the_last_level_has_no_next_threshold() -> None:
    _, _, suivant = gamification.level_for(1000, 0)
    assert suivant is None


def test_the_next_threshold_is_the_one_just_above() -> None:
    _, _, suivant = gamification.level_for(12, 0)
    assert suivant == 30


# --- badges ------------------------------------------------------------------------


async def test_the_first_vote_awards_the_badge(moderator_client, client) -> None:
    _, slug, statements = await open_conversation(moderator_client, statements=3)
    await register(client, "ines@exemple.fr")
    await login(client, "ines@exemple.fr")

    premier = await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": statements[0], "value": 1},
    )

    assert premier.json()["badge_awarded"] == FIRST_VOTE
    badges = (await client.get("/api/me")).json()["progress"]["badges"]
    assert [b["code"] for b in badges] == [FIRST_VOTE]


async def test_the_badge_is_awarded_only_once(
    moderator_client, client, session_factory
) -> None:
    _, slug, statements = await open_conversation(moderator_client, statements=3)
    await register(client, "jules@exemple.fr")
    await login(client, "jules@exemple.fr")

    for statement_id in statements:
        reponse = await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement_id, "value": 1},
        )
        if statement_id != statements[0]:
            assert reponse.json()["badge_awarded"] is None

    async with session_factory() as session:
        total = await session.scalar(
            select(func.count(UserBadge.id)).where(UserBadge.badge_code == FIRST_VOTE)
        )
    assert total == 1


async def test_an_anonymous_visitor_gets_no_badge_and_no_progress(
    moderator_client, client
) -> None:
    """Niveaux et badges appartiennent au compte, conformément à l'indirection."""
    _, slug, statements = await open_conversation(moderator_client, statements=2)

    vote = await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": statements[0], "value": 1},
    )

    assert vote.json()["badge_awarded"] is None
    assert (await client.get("/api/me")).json()["progress"] is None


async def test_an_UNVERIFIED_account_progresses_normally(
    moderator_client, client
) -> None:
    """La vérification d'e-mail n'entre pas dans le calcul : elle ne doit rien bloquer."""
    _, slug, statements = await open_conversation(moderator_client, statements=3)
    await register(client, "kevin@exemple.fr")
    await login(client, "kevin@exemple.fr")

    for statement_id in statements:
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement_id, "value": 1},
        )

    identite = (await client.get("/api/me")).json()
    assert identite["account"]["is_verified"] is False
    assert identite["progress"]["votes"] == 3
    assert identite["progress"]["points"] == 3
    assert FIRST_VOTE in [b["code"] for b in identite["progress"]["badges"]]


async def test_votes_cast_before_the_account_existed_still_count(
    moderator_client, client
) -> None:
    """Conséquence directe du rattachement de C1 : les votes anonymes suivent."""
    _, slug, statements = await open_conversation(moderator_client, statements=3)
    for statement_id in statements:
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement_id, "value": 1},
        )

    await register(client, "laure@exemple.fr")
    await login(client, "laure@exemple.fr")

    assert (await client.get("/api/me")).json()["progress"]["votes"] == 3


async def test_the_profile_page_shows_the_level_and_the_badges(
    moderator_client, client
) -> None:
    _, slug, statements = await open_conversation(moderator_client, statements=2)
    await register(client, "manon@exemple.fr")
    await login(client, "manon@exemple.fr")
    await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": statements[0], "value": 1},
    )

    page = await client.get("/compte")

    assert page.status_code == 200
    assert "Niveau 1" in page.text
    assert "Premier vote" in page.text


async def test_the_profile_page_requires_an_account(client) -> None:
    reponse = await client.get("/compte", follow_redirects=False)

    assert reponse.status_code == 303
    assert "/compte/connexion" in reponse.headers["location"]


async def test_the_catalogue_matches_the_badges_actually_used() -> None:
    codes = {entree["code"] for entree in BADGE_CATALOG}
    assert codes == {FIRST_VOTE, FIRST_CONVERSATION}


async def test_the_vote_response_carries_the_full_badge(
    moderator_client, client
) -> None:
    """L'interface annonce le badge au moment où il est obtenu : elle a donc besoin
    de son intitulé et de son icône, pas seulement du code."""
    _, slug, statements = await open_conversation(moderator_client, statements=3)
    await register(client, "badge@exemple.fr")
    await login(client, "badge@exemple.fr")

    premier = (
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statements[0], "value": 1},
        )
    ).json()
    second = (
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statements[1], "value": 1},
        )
    ).json()

    assert premier["badge"]["code"] == FIRST_VOTE
    assert premier["badge"]["label"] == "Premier vote"
    assert premier["badge"]["icon"]
    assert second["badge"] is None


async def test_an_anonymous_vote_carries_no_badge(moderator_client, client) -> None:
    _, slug, statements = await open_conversation(moderator_client, statements=2)

    reponse = (
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statements[0], "value": 1},
        )
    ).json()

    assert reponse["badge"] is None
    assert reponse["badge_awarded"] is None


def test_the_ring_fills_within_the_current_level_not_from_zero():
    """L'anneau se referme au passage de niveau, puis repart vide.

    Mesurer depuis zéro donnerait un anneau presque plein en permanence : à
    30 points sur un palier suivant à 75, `30/75` afficherait 40 % alors que la
    personne vient d'entrer dans le palier et n'a encore rien parcouru.
    """
    from app.services.gamification import Progress

    def progression(points: int) -> Progress:
        niveau, intitule, suivant = gamification.level_for(points, 0)
        return Progress(
            votes=points,
            conversations=0,
            points=points,
            level=niveau,
            label=intitule,
            next_level_at=suivant,
        )

    # Entrée dans le palier 3 (30 points, suivant à 75) : rien de parcouru.
    assert progression(30).fraction == 0
    # À mi-chemin du palier 3 : (52 - 30) / (75 - 30) = 49 %.
    assert progression(52).fraction == 49
    # Juste avant le palier 4.
    assert progression(74).fraction == 98
    # Tout premier point du site : 1 sur les 10 du palier 1.
    assert progression(1).fraction == 10
    # Dernier palier : plein, il n'y a plus rien à parcourir.
    assert progression(400).next_level_at is None
    assert progression(400).fraction == 100
