"""Sonde de vivacité — ne doit dépendre d'aucune infrastructure."""

from httpx import AsyncClient


async def test_health_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["service"] == "chantier-c-api"


async def test_the_database_probe_answers_get_and_head(client: AsyncClient) -> None:
    """`/health/db` est la sonde qu'un moniteur externe doit interroger.

    `/health` reste à 200 base coupée — c'est voulu, c'est une sonde de vivacité. La
    panne qu'on veut entendre est celle de la base, donc c'est cette route-ci qui est
    surveillée, et elle doit répondre aux deux méthodes : plusieurs services d'uptime
    monitoring sondent en HEAD par défaut, et un 405 s'y lit comme une panne.
    """
    obtenu = await client.get("/health/db")

    assert obtenu.status_code == 200
    assert obtenu.json()["database"] == "ok"

    tete = await client.head("/health/db")

    assert tete.status_code == 200
    assert tete.content == b""
