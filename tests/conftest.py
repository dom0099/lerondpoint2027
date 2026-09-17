"""Infrastructure de test.

Les tests tournent contre une VRAIE base PostgreSQL (`chantierc_test`), distincte de
la base de développement : les comptes, le hachage des mots de passe et les contraintes
d'unicité ne se testent pas sérieusement sur un substitut. Les tables sont recréées
avant chaque test, donc chaque test part d'une base vide.

Aucun e-mail n'est remis au relais : `app.email.outbox` capture les messages, ce qui
permet en plus de récupérer les jetons de réinitialisation et de vérification comme le
ferait un vrai destinataire en cliquant sur le lien.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import email as email_module
from app.config import settings

TEST_DB_NAME = "chantierc_test"
#: URL applicative réelle, mémorisée avant la bascule ci-dessous.
_DEV_DATABASE_URL = settings.database_url
_BASE_URL = settings.database_url.rsplit("/", 1)[0]
TEST_DB_URL = f"{_BASE_URL}/{TEST_DB_NAME}"

# Bascule AVANT d'importer l'application. Tout le code n'accède pas à la base par
# les dépendances FastAPI : l'authentification de sqladmin et la CLI utilisent
# directement `get_sessionmaker()`. Surcharger la seule dépendance `get_session`
# laisserait donc ces chemins pointer vers la base de développement — c'est
# précisément ce qui a fait échouer le premier essai du test de /admin.
# Garde-fou ajouté après l'incident de migration : plus aucune destruction ne doit
# pouvoir viser la base de développement. La cible des tests est dérivée d'une chaîne,
# donc une DATABASE_URL de forme inattendue pourrait la faire pointer ailleurs — cette
# assertion rend la protection structurelle plutôt que dérivée.
assert TEST_DB_URL != settings.database_url, (
    "la base de test coïncide avec la base applicative : "
    f"{TEST_DB_URL} — refus de lancer des tests destructeurs"
)
assert TEST_DB_URL.rsplit("/", 1)[-1] == TEST_DB_NAME

settings.database_url = TEST_DB_URL

from app.db import Base, get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Participant, User  # noqa: E402,F401  (enregistre les tables)


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_database() -> None:
    """Crée la base de test si elle n'existe pas (une fois par session)."""

    async def _create() -> None:
        admin = create_async_engine(f"{_BASE_URL}/postgres", isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"),
                {"n": TEST_DB_NAME},
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
        await admin.dispose()

    asyncio.run(_create())


@pytest.fixture
async def engine():
    eng = create_async_engine(TEST_DB_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    # `create_all` crée les tables mais ne rejoue pas les semis des migrations :
    # sans le catalogue de badges, toute attribution violerait la clé étrangère.
    from app.services.gamification import ensure_badges

    async with async_sessionmaker(eng, expire_on_commit=False)() as session:
        await ensure_badges(session)

    yield eng
    await eng.dispose()


#: Porteur de la fabrique de sessions du test courant, pour les utilitaires qui ne
#: sont pas des fixtures (voir open_conversation plus bas).
_SESSION_FACTORY: list = [None]


@pytest.fixture
async def session_factory(engine) -> async_sessionmaker:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    _SESSION_FACTORY[0] = factory
    return factory


@pytest.fixture
async def client_factory(session_factory) -> AsyncIterator[Callable]:
    """Fabrique de clients HTTP indépendants.

    Chaque client a son propre bocal à cookies : c'est ce qui permet de simuler
    deux navigateurs distincts (indispensable pour tester le rattachement).
    """

    async def override_get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session

    @asynccontextmanager
    async def _make() -> AsyncIterator[AsyncClient]:
        # base_url en **https** : depuis le passage en production, les cookies de
        # session et de participant portent l'attribut `Secure`, et httpx refuse
        # d'en garder un reçu sur une origine http. Sans cela, toute la suite se
        # comporte comme si personne n'était jamais connecté.
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            yield client

    yield _make
    app.dependency_overrides.clear()


@pytest.fixture
async def client(client_factory) -> AsyncIterator[AsyncClient]:
    async with client_factory() as c:
        yield c


@pytest.fixture(autouse=True)
async def reset_global_engine():
    """Libère le moteur global entre deux tests.

    Tout ne passe pas par la dépendance `get_session` : sqladmin et la CLI utilisent
    `get_engine()`, mis en cache par `lru_cache` pour toute la durée du processus. Une
    connexion ouverte dans la boucle d'événements d'un test puis réutilisée dans celle
    du suivant lève « attached to a different loop » — c'est ce qui faisait échouer un
    second test touchant /admin, quel qu'il soit.
    """
    yield
    from app.db import get_engine

    # Le pool est vidé, mais l'objet moteur est CONSERVÉ : sqladmin a capté celui-ci
    # au montage de l'application. Vider le cache `lru_cache` en fabriquerait un
    # second, et l'admin continuerait de traîner les connexions du premier.
    if get_engine.cache_info().currsize:
        await get_engine().dispose()


@pytest.fixture(autouse=True)
def outbox():
    """Capture les e-mails au lieu de les remettre au relais."""
    email_module.outbox = []
    yield email_module.outbox
    email_module.outbox = None


# --- utilitaires partagés -------------------------------------------------------

PASSWORD = "MotDePasse!2027"


async def register(client: AsyncClient, email: str, password: str = PASSWORD):
    return await client.post(
        "/auth/register", json={"email": email, "password": password}
    )


async def login(client: AsyncClient, email: str, password: str = PASSWORD):
    return await client.post(
        "/auth/login", data={"username": email, "password": password}
    )


def token_from(message) -> str:
    """Extrait le jeton du lien contenu dans un e-mail, comme le ferait un clic."""
    body = message.get_content()
    for word in body.split():
        if "token=" in word:
            return word.split("token=", 1)[1].strip()
    raise AssertionError(f"aucun jeton trouvé dans l'e-mail :\n{body}")


# --- modérateur -----------------------------------------------------------------

MODERATOR_EMAIL = "moderateur@exemple.fr"


@pytest.fixture
async def moderator(session_factory):
    """Un compte is_superuser, créé par le même service que la CLI."""
    from app.services.accounts import ensure_superuser

    async with session_factory() as session:
        user, _ = await ensure_superuser(session, MODERATOR_EMAIL, PASSWORD, "Modérateur")
        return user


@pytest.fixture
async def moderator_client(client_factory, moderator):
    """Client déjà connecté sur les pages de modération."""
    async with client_factory() as client:
        response = await client.post(
            "/moderation/login",
            data={"username": MODERATOR_EMAIL, "password": PASSWORD},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        yield client


async def create_conversation(moderator_client, title="Conversation d'essai", mode="pre"):
    """Crée une conversation via l'écran de modération et renvoie son id."""
    response = await moderator_client.post(
        "/moderation/conversations",
        data={"title": title, "description": "", "moderation_mode": mode},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


async def depublier(session_factory, statement_id, statut=None):
    """Sort une proposition de la publication, sans passer par un écran (MOD-14).

    **Pourquoi ce détour existe.** Depuis le MOD-14 une proposition déposée naît
    `approved` : l'état non publié ne s'obtient plus par le dépôt. Il existe pourtant
    toujours — retrait conservatoire après signalement, amorce d'une conversation encore
    en attente — et les règles qui le concernent (ni votée, ni servie, ni recensée, ni
    interrogée) doivent rester éprouvées. On pose donc l'état directement, plutôt que de
    le fabriquer par un chemin qui n'existe plus.
    """
    from app.models import ModerationStatus, Statement

    async with session_factory() as session:
        statement = await session.get(Statement, statement_id)
        statement.moderation_status = statut or ModerationStatus.pending
        await session.commit()
    return statement_id


async def open_conversation(
    moderator_client,
    *,
    statements=3,
    title="Conversation ouverte",
    mode="pre",
    allow=True,
    themes=("institutions",),
):
    """Crée une conversation OUVERTE avec ses propositions d'amorce.

    Renvoie (id, slug, [ids des propositions]).
    """
    conversation_id = await create_conversation(moderator_client, title=title, mode=mode)
    # Un thème est exigé dès la publication depuis le chantier J5 : ouvrir une
    # conversation sans en poser un est refusé. `themes` est un paramètre pour que les
    # tests qui s'y intéressent choisissent, et un défaut pour les autres — ils sont
    # nombreux, et l'étiquetage ne les regarde pas.
    data = {
        "title": title,
        "description": "",
        "state": "open",
        "moderation_mode": mode,
        "themes": list(themes),
    }
    if allow:
        data["allow_participant_statements"] = "1"
    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}", data=data, follow_redirects=False
    )
    for index in range(statements):
        await moderator_client.post(
            f"/moderation/conversations/{conversation_id}/statements",
            data={"text": f"Proposition d'amorce n°{index + 1}."},
            follow_redirects=False,
        )

    from sqlalchemy import select

    from app.models import ModerationStatus, Statement

    async with _SESSION_FACTORY[0]() as session:
        ids = list(
            await session.scalars(
                select(Statement.id)
                .where(
                    Statement.conversation_id == conversation_id,
                    Statement.moderation_status == ModerationStatus.approved,
                )
                .order_by(Statement.id)
            )
        )

    slug = None
    for entry in (await moderator_client.get("/api/conversations")).json():
        if entry["title"] == title:
            slug = entry["slug"]
    return conversation_id, slug, ids
