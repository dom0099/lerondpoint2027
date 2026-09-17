"""Situations limites relevées par la revue, et corrections qui en découlent.

Chaque test porte le nom du défaut qu'il empêche de revenir. Les six premiers
couvrent des comportements qui étaient corrects mais que rien ne vérifiait — un
comportement juste et non testé n'est juste que jusqu'au prochain changement.
"""

import asyncio

import pytest
from fastapi_users.jwt import generate_jwt
from sqlalchemy import func, select

from app.models import AnalysisStatus, Conversation, Participant, Statement, User, Vote
from app.models.gamification import FIRST_CONVERSATION
from tests.conftest import (
    PASSWORD,
    login,
    open_conversation,
    register,
    token_from,
)


# --- 1. comportements corrects mais non testés -----------------------------------


async def test_html_is_escaped_in_rendered_pages(moderator_client, client) -> None:
    """Une proposition hostile ne doit pas devenir du code exécutable.

    Éprouvé sur l'éditeur de débat depuis le MOD-14 : la file de modération qui portait
    ce test a été retirée avec la pré-modération. C'est le dernier écran rendu par le
    SERVEUR qui affiche le texte brut d'une proposition — la page de vote, elle, le reçoit
    en JSON et le pose par le navigateur, où il n'est jamais interprété comme du HTML.
    """
    conversation_id, slug, _ = await open_conversation(moderator_client)
    await client.post(
        f"/api/conversations/{slug}/statements",
        json={"text": "<script>alert(1)</script> <img src=x onerror=alert(2)>"},
    )

    page = await moderator_client.get(f"/moderation/conversations/{conversation_id}")

    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;" in page.text
    assert "&lt;img" in page.text


async def test_an_expired_reset_token_is_refused(client, session_factory) -> None:
    await register(client, "expire@exemple.fr")
    async with session_factory() as session:
        from app.auth.service import user_manager_for

        manager = user_manager_for(session)
        user = await manager.user_db.get_by_email("expire@exemple.fr")
        perime = generate_jwt(
            {
                "sub": str(user.id),
                "password_fgpt": manager.password_helper.hash(user.hashed_password),
                "aud": manager.reset_password_token_audience,
            },
            manager.reset_password_token_secret,
            -10,  # déjà expiré à l'émission
        )

    reponse = await client.post(
        "/compte/reinitialisation",
        data={"token": perime, "password": "Nouveau!2027", "confirmation": "Nouveau!2027"},
    )

    assert "plus valable" in reponse.text
    assert (await login(client, "expire@exemple.fr", PASSWORD)).status_code == 204


async def test_a_reset_token_cannot_be_replayed(client_factory, outbox) -> None:
    """Le jeton est invalidé par le changement de mot de passe lui-même."""
    async with client_factory() as navigateur:
        await register(navigateur, "rejeu@exemple.fr")
        outbox.clear()
        await navigateur.post(
            "/compte/mot-de-passe-oublie",
            data={"username": "rejeu@exemple.fr"},
            follow_redirects=True,
        )
        jeton = token_from(outbox[0])

        premier = await navigateur.post(
            "/compte/reinitialisation",
            data={"token": jeton, "password": "Premier!2027", "confirmation": "Premier!2027"},
        )
        second = await navigateur.post(
            "/compte/reinitialisation",
            data={"token": jeton, "password": "Second!2027", "confirmation": "Second!2027"},
        )

    assert "changé" in premier.text
    assert "plus valable" in second.text
    async with client_factory() as frais:
        assert (await login(frais, "rejeu@exemple.fr", "Premier!2027")).status_code == 204
        assert (await login(frais, "rejeu@exemple.fr", "Second!2027")).status_code == 400


async def test_a_failure_while_saving_does_not_leave_a_run_running(
    moderator_client, session_factory, monkeypatch
) -> None:
    """Le run doit finir en `error`, jamais rester figé en `running`.

    Un run figé serait invisible dans les résultats mais s'accumulerait en base, et
    rien ne le reprendrait.
    """
    from app.analysis import pipeline

    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8
    )

    async def _echoue(*args, **kwargs):
        raise RuntimeError("écriture impossible")

    async with session_factory() as session:
        for index in range(6):
            participant = Participant(anon_token=f"jeton-panne-{index}")
            session.add(participant)
            await session.flush()
            for position, statement_id in enumerate(statements):
                session.add(
                    Vote(
                        participant_id=participant.id,
                        statement_id=statement_id,
                        value=1 if position % 2 == index % 2 else -1,
                    )
                )
        await session.commit()

        monkeypatch.setattr(pipeline, "_persist", _echoue)
        conversation = await session.get(Conversation, conversation_id)
        run = await pipeline.analyse(session, conversation)

    assert run.status is AnalysisStatus.error
    assert "écriture impossible" in run.error_text
    assert run.finished_at is not None


async def test_a_session_survives_nothing_once_the_account_is_deleted(
    client, session_factory
) -> None:
    await register(client, "supprime@exemple.fr")
    await login(client, "supprime@exemple.fr")
    assert (await client.get("/users/me")).status_code == 200

    async with session_factory() as session:
        user = await session.scalar(
            select(User).where(User.email == "supprime@exemple.fr")
        )
        await session.delete(user)
        await session.commit()

    # Le JWT reste cryptographiquement valide, mais ne désigne plus personne :
    # la session doit retomber en anonyme, pas donner accès.
    assert (await client.get("/users/me")).status_code == 401
    identite = (await client.get("/api/me")).json()
    assert identite["authenticated"] is False
    assert identite["progress"] is None


async def test_simultaneous_votes_create_a_single_row(
    moderator_client, client, session_factory
) -> None:
    """12 votes concurrents du même participant sur la même proposition."""
    _, slug, statements = await open_conversation(moderator_client, statements=2)
    cible = statements[0]
    await client.get("/api/me")  # fixe l'identité avant la rafale

    await asyncio.gather(
        *[
            client.post(
                f"/api/conversations/{slug}/votes",
                json={"statement_id": cible, "value": (index % 3) - 1},
            )
            for index in range(12)
        ]
    )

    async with session_factory() as session:
        lignes = await session.scalar(
            select(func.count(Vote.id)).where(Vote.statement_id == cible)
        )
    assert lignes == 1


# --- 2. corrections ---------------------------------------------------------------


async def test_the_merge_keeps_the_credit_of_a_proposed_conversation(
    moderator_client, client_factory, session_factory
) -> None:
    """Le défaut le plus coûteux de la revue : après fusion, la conversation
    proposée n'appartenait plus à personne — ni points, ni badge, ni auteur."""
    async with client_factory() as navigateur_a:
        await register(navigateur_a, "fusion@exemple.fr")
        await login(navigateur_a, "fusion@exemple.fr")
        compte = (await navigateur_a.get("/api/me")).json()["participant_id"]

    async with client_factory() as navigateur_b:
        anonyme = (await navigateur_b.get("/api/me")).json()["participant_id"]
        await navigateur_b.post(
            "/proposer",
            data={
                "title": "Proposee avant fusion",
                "description": "",
                "statements": ["Une.", "Deux.", "Trois."],
            },
            follow_redirects=True,
        )
        await login(navigateur_b, "fusion@exemple.fr")

    async with session_factory() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.title == "Proposee avant fusion")
        )
    assert anonyme != compte
    # La conversation suit le participant fusionné.
    assert conversation.proposed_by_participant_id == compte

    await moderator_client.post(
        f"/moderation/queue/conversations/{conversation.id}/approve",
        data={"themes": ["institutions"]},
        follow_redirects=False,
    )

    async with client_factory() as navigateur_c:
        await login(navigateur_c, "fusion@exemple.fr")
        progression = (await navigateur_c.get("/api/me")).json()["progress"]

    assert progression["conversations"] == 1
    assert progression["points"] == 25
    assert FIRST_CONVERSATION in [b["code"] for b in progression["badges"]]


async def test_password_reset_requests_are_capped(client, outbox) -> None:
    """Sans plafond, l'endpoint permet d'épuiser le quota d'envoi du relais."""
    from app.services.rate_limit import RESET_PER_EMAIL

    await register(client, "plafond@exemple.fr")
    outbox.clear()

    for _ in range(RESET_PER_EMAIL + 3):
        reponse = await client.post(
            "/compte/mot-de-passe-oublie",
            data={"username": "plafond@exemple.fr"},
            follow_redirects=True,
        )
        # Le refus est silencieux : la page ne dit jamais si un e-mail est parti.
        assert "valable une heure" in reponse.text

    assert len(outbox) == RESET_PER_EMAIL


async def test_an_already_moderated_conversation_cannot_be_re_approved(
    moderator_client, client, session_factory
) -> None:
    """Rejouer un lien d'approbation rouvrait une conversation close."""
    await client.post(
        "/proposer",
        data={
            "title": "Deja moderee",
            "description": "",
            "statements": ["Une.", "Deux.", "Trois."],
        },
        follow_redirects=True,
    )
    async with session_factory() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.title == "Deja moderee")
        )

    premier = await moderator_client.post(
        f"/moderation/queue/conversations/{conversation.id}/approve",
        data={"themes": ["institutions"]},
        follow_redirects=False,
    )
    # Le modérateur ferme ensuite la consultation.
    await moderator_client.post(
        f"/moderation/conversations/{conversation.id}",
        data={
            "title": "Deja moderee",
            "description": "",
            "state": "closed",
            "themes": ["institutions"],
            "moderation_mode": "pre",
        },
        follow_redirects=False,
    )
    rejeu = await moderator_client.post(
        f"/moderation/queue/conversations/{conversation.id}/approve",
        data={"themes": ["institutions"]},
        follow_redirects=False,
    )

    assert premier.status_code == 303
    assert rejeu.status_code == 409
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation.id)
    assert conversation.state.value == "closed"  # toujours close


async def test_simultaneous_registrations_do_not_return_a_server_error(
    client_factory,
) -> None:
    """Deux inscriptions concurrentes : un compte, et un refus explicite."""
    async with client_factory() as a, client_factory() as b:
        reponses = await asyncio.gather(
            a.post(
                "/auth/register",
                json={"email": "course@exemple.fr", "password": PASSWORD},
            ),
            b.post(
                "/auth/register",
                json={"email": "course@exemple.fr", "password": PASSWORD},
            ),
            return_exceptions=True,
        )

    codes = sorted(r.status_code for r in reponses if not isinstance(r, Exception))
    assert 500 not in codes
    assert 201 in codes


async def test_simultaneous_proposals_with_the_same_title_all_succeed(
    client_factory, session_factory
) -> None:
    """La course sur le slug renvoyait 500 ; elle doit se résoudre par un suffixe."""
    donnees = {
        "title": "Titre en course",
        "description": "",
        "statements": ["Une.", "Deux.", "Trois."],
    }
    async with client_factory() as a, client_factory() as b, client_factory() as c:
        reponses = await asyncio.gather(
            a.post("/proposer", data=donnees, follow_redirects=True),
            b.post("/proposer", data=donnees, follow_redirects=True),
            c.post("/proposer", data=donnees, follow_redirects=True),
            return_exceptions=True,
        )

    assert [r.status_code for r in reponses if not isinstance(r, Exception)] == [200] * 3
    async with session_factory() as session:
        slugs = list(
            await session.scalars(
                select(Conversation.slug).where(Conversation.title == "Titre en course")
            )
        )
    assert len(slugs) == 3
    assert len(set(slugs)) == 3  # trois slugs distincts


async def test_an_overlong_title_gets_a_message_not_a_server_error(client) -> None:
    reponse = await client.post(
        "/proposer",
        data={
            "title": "T" * 400,
            "description": "",
            "statements": ["Une.", "Deux.", "Trois."],
        },
    )

    assert reponse.status_code == 200
    assert "300 caractères" in reponse.text


async def test_a_moderator_statement_is_length_checked_too(moderator_client) -> None:
    """Le chemin modérateur acceptait 5000 caractères là où l'API refuse au-delà de 500."""
    conversation_id, _, _ = await open_conversation(moderator_client)

    reponse = await moderator_client.post(
        f"/moderation/conversations/{conversation_id}/statements",
        data={"text": "b" * 5000},
        follow_redirects=False,
    )

    assert reponse.status_code == 422


async def test_the_same_statement_cannot_be_proposed_twice(
    moderator_client, client, session_factory
) -> None:
    _, slug, _ = await open_conversation(moderator_client)
    texte = {"text": "Exactement la même proposition."}

    premier = await client.post(f"/api/conversations/{slug}/statements", json=texte)
    second = await client.post(f"/api/conversations/{slug}/statements", json=texte)

    assert premier.status_code == 201
    assert second.status_code == 409
    async with session_factory() as session:
        total = await session.scalar(
            select(func.count(Statement.id)).where(
                Statement.text == "Exactement la même proposition."
            )
        )
    assert total == 1


async def test_simultaneous_duplicate_statements_create_only_one(
    moderator_client, client, session_factory
) -> None:
    """La contrainte porte la règle : même en rafale, une seule ligne."""
    _, slug, _ = await open_conversation(moderator_client)
    await client.get("/api/me")

    await asyncio.gather(
        *[
            client.post(
                f"/api/conversations/{slug}/statements",
                json={"text": "Double clic sur proposer."},
            )
            for _ in range(6)
        ],
        return_exceptions=True,
    )

    async with session_factory() as session:
        total = await session.scalar(
            select(func.count(Statement.id)).where(
                Statement.text == "Double clic sur proposer."
            )
        )
    assert total == 1


async def test_duplicate_seed_statements_in_one_proposal_are_refused(client) -> None:
    reponse = await client.post(
        "/proposer",
        data={
            "title": "Doublons internes",
            "description": "",
            "statements": ["La même.", "La même.", "La même."],
        },
    )

    assert "identiques" in reponse.text


@pytest.mark.parametrize(
    ("chemin", "donnees"),
    [
        ("/proposer", {"title": "PRG", "description": "",
                       "statements": ["Une.", "Deux.", "Trois."]}),
        ("/compte/mot-de-passe-oublie", {"username": "inconnu@exemple.fr"}),
    ],
)
async def test_successful_posts_redirect_so_a_refresh_is_harmless(
    client, chemin, donnees
) -> None:
    """Sans POST/Redirect/GET, un F5 renvoie le formulaire : seconde proposition,
    ou second e-mail."""
    reponse = await client.post(chemin, data=donnees, follow_redirects=False)

    assert reponse.status_code == 303
    assert "envoye=1" in reponse.headers["location"]


# --- plafonds sur les propositions ------------------------------------------------


async def test_conversation_proposals_are_capped(client, session_factory) -> None:
    """Rien ne bornait le nombre de conversations qu'une personne peut proposer.

    Le refus est ici EXPLICITE, contrairement au mot de passe oublié : il n'y a rien
    à révéler sur l'existence d'un compte, et un envoi silencieusement ignoré ferait
    croire à une proposition enregistrée.
    """
    from app.services.rate_limit import CONVERSATIONS_PER_PARTICIPANT

    reponses = []
    for numero in range(CONVERSATIONS_PER_PARTICIPANT + 1):
        reponses.append(
            await client.post(
                "/proposer",
                data={
                    "title": f"Proposition numero {numero}",
                    "description": "",
                    "statements": ["Une.", "Deux.", "Trois."],
                },
                follow_redirects=True,
            )
        )

    async with session_factory() as session:
        enregistrees = await session.scalar(select(func.count(Conversation.id)))

    assert enregistrees == CONVERSATIONS_PER_PARTICIPANT
    assert "limite de propositions" in reponses[-1].text
    # Le texte saisi est conservé : la personne n'a pas à tout retaper demain.
    assert "Proposition numero 3" in reponses[-1].text


async def test_statement_proposals_are_capped_per_conversation(
    moderator_client, client
) -> None:
    """Le seau inclut la conversation.

    Quelqu'un qui participe activement à trois consultations n'est pas quelqu'un qui
    inonde la file de l'une d'elles.
    """
    from app.services.rate_limit import STATEMENTS_PER_PARTICIPANT

    _, premier, _ = await open_conversation(moderator_client, title="Premiere consultation")
    _, second, _ = await open_conversation(moderator_client, title="Seconde consultation")

    reponses = []
    for numero in range(STATEMENTS_PER_PARTICIPANT + 1):
        reponses.append(
            await client.post(
                f"/api/conversations/{premier}/statements",
                json={"text": f"Proposition de proposition n°{numero}."},
            )
        )
    ailleurs = await client.post(
        f"/api/conversations/{second}/statements",
        json={"text": "Une proposition dans une autre consultation."},
    )

    assert [r.status_code for r in reponses[:-1]] == [201] * STATEMENTS_PER_PARTICIPANT
    assert reponses[-1].status_code == 429
    assert "24 heures" in reponses[-1].json()["detail"]
    # La conversation voisine n'est pas touchée.
    assert ailleurs.status_code == 201


# --- coupure de base ---------------------------------------------------------------


async def test_a_database_outage_answers_503_and_not_a_raw_500(client_factory) -> None:
    """Une base injoignable est une indisponibilité, pas un bug applicatif.

    La base visée n'existe pas : asyncpg refuse la connexion avant tout
    enveloppement par SQLAlchemy — c'est exactement ce qui remonte quand le serveur
    est arrêté, et c'est ce qu'un gestionnaire posé sur le seul `DBAPIError` aurait
    laissé passer en 500.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import get_session
    from app.main import app
    from tests.conftest import TEST_DB_URL

    moteur = create_async_engine(f"{TEST_DB_URL.rsplit('/', 1)[0]}/base_qui_nexiste_pas")

    async def base_injoignable():
        async with async_sessionmaker(moteur)() as session:
            yield session

    async with client_factory() as client:
        app.dependency_overrides[get_session] = base_injoignable
        api = await client.get("/api/conversations")
        page = await client.get("/")
        sonde = await client.get("/health")
    await moteur.dispose()

    # L'interface de vote attend du JSON : lui renvoyer une page HTML la ferait
    # échouer au parsage.
    assert api.status_code == 503
    assert api.headers["retry-after"] == "15"
    assert "indisponible" in api.json()["detail"]

    assert page.status_code == 503
    assert page.headers["retry-after"] == "15"
    assert "Service momentanément indisponible" in page.text

    # La sonde de vivacité ne dépend d'aucune infrastructure : elle reste verte.
    assert sonde.status_code == 200


# --- derrière le proxy : vraie adresse du visiteur ---------------------------------


async def _seaux(session_factory, prefixe: str) -> set[str]:
    from app.models import RateLimitHit

    async with session_factory() as session:
        seaux = await session.scalars(select(RateLimitHit.bucket))
    return {b for b in seaux if b.startswith(prefixe)}


async def test_the_real_visitor_ip_is_read_from_the_proxy_header(
    client_factory, session_factory
) -> None:
    """Derrière nginx, `request.client.host` vaut l'adresse du PROXY pour tout le
    monde : les plafonds par IP deviendraient un plafond global. uvicorn réécrit
    l'adresse depuis `X-Forwarded-For`, mais seulement pour une source déclarée dans
    `--forwarded-allow-ips` (voir docker-compose.yml)."""
    from httpx import ASGITransport, AsyncClient
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from app.main import app

    derriere_proxy = ProxyHeadersMiddleware(app, trusted_hosts="172.30.0.0/24")

    async with AsyncClient(
        transport=ASGITransport(app=derriere_proxy, client=("172.30.0.9", 0)),
        base_url="https://test",
    ) as visiteur:
        await visiteur.post(
            "/auth/login",
            data={"username": "quelquun@exemple.fr", "password": "x"},
            headers={"X-Forwarded-For": "203.0.113.7"},
        )

    assert await _seaux(session_factory, "login-ip:") == {"login-ip:203.0.113.7"}


async def test_a_forged_header_from_an_untrusted_source_is_ignored(
    client_factory, session_factory
) -> None:
    """Sinon le plafond se contourne en une ligne de curl : il suffirait d'envoyer
    soi-même l'en-tête pour se choisir une adresse neuve à chaque tentative."""
    from httpx import ASGITransport, AsyncClient
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from app.main import app

    derriere_proxy = ProxyHeadersMiddleware(app, trusted_hosts="172.30.0.0/24")

    async with AsyncClient(
        transport=ASGITransport(app=derriere_proxy, client=("10.9.9.9", 0)),
        base_url="https://test",
    ) as menteur:
        await menteur.post(
            "/auth/login",
            data={"username": "quelquun@exemple.fr", "password": "x"},
            headers={"X-Forwarded-For": "203.0.113.7"},
        )

    # L'adresse retenue est celle de la connexion, pas celle annoncée.
    assert await _seaux(session_factory, "login-ip:") == {"login-ip:10.9.9.9"}


# --- conservation bornée des traces de plafond ------------------------------------


async def test_expired_traces_are_purged_even_without_any_traffic(
    session_factory,
) -> None:
    """Le nettoyage paresseux ne balayait que le seau qu'il venait de toucher.

    Une ligne `reset:<adresse e-mail>` d'un seau jamais re-sollicité restait donc en
    base indéfiniment — une donnée personnelle sans durée de conservation réelle.
    """
    from datetime import datetime, timedelta, timezone

    from app.models import RateLimitHit
    from app.services import rate_limit

    maintenant = datetime.now(timezone.utc)
    async with session_factory() as session:
        session.add_all(
            [
                RateLimitHit(
                    bucket="reset:oublie@exemple.fr",
                    created_at=maintenant - timedelta(days=3),
                ),
                RateLimitHit(
                    bucket="login-ip:198.51.100.4",
                    created_at=maintenant - timedelta(days=2),
                ),
                RateLimitHit(
                    bucket="conv-ip:198.51.100.4",
                    created_at=maintenant - timedelta(minutes=5),
                ),
            ]
        )
        await session.commit()

        supprimees = await rate_limit.purge_expired(session)
        restantes = list(await session.scalars(select(RateLimitHit.bucket)))

    assert supprimees == 2
    assert restantes == ["conv-ip:198.51.100.4"]


async def test_the_worker_purges_the_traces_on_every_pass(session_factory) -> None:
    """Le balayage est confié au worker pour avoir lieu même sans visiteur."""
    from datetime import datetime, timedelta, timezone

    from app import worker
    from app.models import RateLimitHit

    async with session_factory() as session:
        session.add(
            RateLimitHit(
                bucket="reset:vieux@exemple.fr",
                created_at=datetime.now(timezone.utc) - timedelta(days=5),
            )
        )
        await session.commit()

    await worker.run_once()

    async with session_factory() as session:
        assert list(await session.scalars(select(RateLimitHit.bucket))) == []
