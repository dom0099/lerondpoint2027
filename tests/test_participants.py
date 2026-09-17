"""Participants : anonymat, rattachement à un compte, et accès sans vérification.

C'est ici que se joue le point le plus sensible de C1 : `get_current_participant` est
la dépendance que l'endpoint de vote de C3 utilisera telle quelle. Ces tests
établissent qui peut l'emprunter — et en particulier qu'un compte NON VÉRIFIÉ le peut.
"""

from httpx import AsyncClient

from app.auth.users import ANON_COOKIE_NAME
from tests.conftest import login, register, token_from


async def test_anonymous_visitor_gets_a_participant_and_a_signed_cookie(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/me")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["participant_id"], int)
    assert body["authenticated"] is False
    assert body["account"] is None
    assert ANON_COOKIE_NAME in response.cookies


async def test_anonymous_participant_is_stable_across_requests(
    client: AsyncClient,
) -> None:
    first = (await client.get("/api/me")).json()["participant_id"]
    second = (await client.get("/api/me")).json()["participant_id"]

    assert first == second


async def test_two_visitors_get_two_participants(client_factory) -> None:
    async with client_factory() as a, client_factory() as b:
        first = (await a.get("/api/me")).json()["participant_id"]
        second = (await b.get("/api/me")).json()["participant_id"]

    assert first != second


async def test_forged_anon_cookie_is_ignored(client: AsyncClient) -> None:
    """Un cookie non signé correctement doit être traité comme absent."""
    client.cookies.set(ANON_COOKIE_NAME, "jeton-bidon")

    response = await client.get("/api/me")

    assert response.status_code == 200
    assert response.json()["participant_id"] > 0


async def test_UNVERIFIED_account_can_use_the_participant_route(
    client: AsyncClient,
) -> None:
    """LE test sensible de C1.

    Un compte dont l'e-mail n'est pas confirmé doit pouvoir agir en tant que
    participant — c'est-à-dire voter, dès que C3 branchera l'endpoint de vote sur
    cette même dépendance `get_current_participant`.
    """
    await register(client, "hugo@exemple.fr")
    assert (await login(client, "hugo@exemple.fr")).status_code == 204

    response = await client.get("/api/me")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authenticated"] is True
    assert body["account"]["is_verified"] is False  # bien NON vérifié
    assert isinstance(body["participant_id"], int)  # et pourtant identifié


async def test_verified_account_can_use_the_participant_route_too(
    client: AsyncClient, outbox
) -> None:
    """Contrepoint du test précédent : la vérification n'enlève rien non plus."""
    await register(client, "iris@exemple.fr")
    await client.post("/auth/verify", json={"token": token_from(outbox[0])})
    await login(client, "iris@exemple.fr")

    response = await client.get("/api/me")

    assert response.status_code == 200
    assert response.json()["account"]["is_verified"] is True
    assert response.json()["authenticated"] is True


async def test_anonymous_participant_is_claimed_when_the_account_is_created(
    client: AsyncClient,
) -> None:
    """Le point de conception central : l'historique suit le participant.

    Quelqu'un participe en anonyme, puis crée un compte : il doit garder LE MÊME
    `participant_id`. Sinon, rendre les comptes obligatoires plus tard ferait perdre
    tous les votes émis en anonyme.
    """
    anonymous_id = (await client.get("/api/me")).json()["participant_id"]

    await register(client, "julie@exemple.fr")
    await login(client, "julie@exemple.fr")

    body = (await client.get("/api/me")).json()
    assert body["authenticated"] is True
    assert body["participant_id"] == anonymous_id


async def test_account_keeps_its_participant_when_logging_in_elsewhere(
    client_factory,
) -> None:
    """Limitation connue, explicitée par un test.

    Depuis un autre navigateur, un participant anonyme existe déjà. À la connexion,
    c'est le participant DU COMPTE qui l'emporte : le compte ne peut pas en avoir
    deux, sans quoi il pèserait double dans l'analyse. Le participant anonyme de ce
    navigateur est abandonné — sans conséquence à C1 puisqu'aucun vote n'existe
    encore, mais **à C3 ce cas devra fusionner les votes**.
    """
    async with client_factory() as first_browser:
        await register(first_browser, "karim@exemple.fr")
        # L'inscription ne connecte pas : sans cette connexion, /api/me répondrait
        # en anonyme et on lirait le mauvais participant.
        await login(first_browser, "karim@exemple.fr")
        account_participant = (await first_browser.get("/api/me")).json()[
            "participant_id"
        ]

    async with client_factory() as second_browser:
        other_anonymous = (await second_browser.get("/api/me")).json()["participant_id"]
        assert other_anonymous != account_participant

        await login(second_browser, "karim@exemple.fr")
        body = (await second_browser.get("/api/me")).json()

    assert body["participant_id"] == account_participant
