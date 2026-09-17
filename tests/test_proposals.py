"""Conversations proposées par les participants.

Ouverture de périmètre décidée à C5 : la création de conversation n'est plus réservée
aux modérateurs. Même principe que les propositions — pré-modération, donc invisible
tant qu'un modérateur n'a pas approuvé.
"""

from sqlalchemy import select

from app.models import Conversation, ModerationStatus, Statement, User
from app.models.gamification import FIRST_CONVERSATION
from app.services import conversations as service
from tests.conftest import login, register


async def propose(client, titre="Les écoles du quartier", propositions=None, description=""):
    # follow_redirects : depuis la revue des situations limites, une proposition
    # acceptée répond 303 vers /proposer?envoye=1 (POST/Redirect/GET), pour qu'un F5
    # ne crée pas une seconde proposition.
    return await client.post(
        "/proposer",
        follow_redirects=True,
        data={
            "title": titre,
            "description": description,
            "statements": propositions
            or [
                "Les cantines devraient être gratuites.",
                "Chaque école doit avoir une cour végétalisée.",
                "Les devoirs à la maison sont à supprimer.",
            ],
        },
    )


# --- la proposition ----------------------------------------------------------------


async def test_a_participant_can_propose_a_conversation(client, session_factory) -> None:
    reponse = await propose(client)

    assert reponse.status_code == 200
    assert "après relecture" in reponse.text
    async with session_factory() as session:
        conversation = await session.scalar(select(Conversation))
        propositions = list(await session.scalars(select(Statement)))

    assert conversation.moderation_status is ModerationStatus.pending
    assert conversation.state.value == "draft"
    assert len(propositions) == 3
    assert all(s.moderation_status is ModerationStatus.pending for s in propositions)
    assert all(s.is_seed for s in propositions)


async def test_a_pending_conversation_is_invisible_everywhere(client) -> None:
    await propose(client)

    accueil = await client.get("/")
    listing = await client.get("/api/conversations")

    assert "Les écoles du quartier" not in accueil.text
    assert listing.json() == []


async def test_an_anonymous_visitor_can_propose_too(client, session_factory) -> None:
    """Même contrat que le vote : aucun compte exigé. Sans compte, simplement pas de
    badge ni de niveau."""
    participant = (await client.get("/api/me")).json()["participant_id"]

    await propose(client)

    async with session_factory() as session:
        conversation = await session.scalar(select(Conversation))
    assert conversation.proposed_by_participant_id == participant
    assert conversation.owner_user_id is None


async def test_too_few_statements_is_refused(client) -> None:
    reponse = await propose(client, propositions=["Une seule."])

    assert "au moins 3" in reponse.text


async def test_too_many_statements_is_refused(client) -> None:
    reponse = await propose(client, propositions=[f"Proposition {i}." for i in range(11)])

    assert "Pas plus de 10" in reponse.text


async def test_an_empty_title_is_refused(client) -> None:
    reponse = await propose(client, titre="   ")

    assert "titre est nécessaire" in reponse.text


async def test_blank_statements_do_not_count_towards_the_minimum(client) -> None:
    reponse = await propose(client, propositions=["Une vraie.", "   ", ""])

    assert "au moins 3" in reponse.text


# --- la file de modération ---------------------------------------------------------


async def test_la_file_ne_porte_plus_que_les_conversations_et_les_noms(
    moderator_client, client
) -> None:
    """Depuis le MOD-14, la file n'a plus de section « propositions » : une proposition
    déposée est publiée d'emblée. Ce qui s'y décide encore est d'un autre ordre —
    approuver une conversation ouvre un espace entier, et un nom de groupe porte sur des
    personnes."""
    from tests.conftest import open_conversation

    _, slug, _ = await open_conversation(moderator_client, title="Déjà publiée")
    await client.post(
        f"/api/conversations/{slug}/statements", json={"text": "Une proposition seule."}
    )
    await propose(client)

    file = await moderator_client.get("/moderation/queue")

    assert "Conversations proposées (1)" in file.text
    assert "Les écoles du quartier" in file.text
    # Ni la section, ni la proposition : elle est en ligne, pas en attente.
    assert "Propositions en attente" not in file.text
    assert "Une proposition seule." not in file.text


async def test_les_amorces_d_une_conversation_en_attente_restent_en_attente(
    moderator_client, client, session_factory
) -> None:
    """La règle que la file protégeait autrefois par omission tient toujours, et elle
    compte plus que jamais : **une amorce n'est pas publiée d'emblée**, elle se juge avec
    sa conversation. Sans quoi proposer un débat publierait ses propositions avant que
    quiconque ait accepté le débat lui-même."""
    await propose(client)

    async with session_factory() as session:
        amorces = list(
            await session.scalars(select(Statement).where(Statement.is_seed.is_(True)))
        )

    assert amorces
    assert all(s.moderation_status is ModerationStatus.pending for s in amorces)
    # Et elles se lisent dans la carte de leur conversation, pas dans une file à part.
    file = await moderator_client.get("/moderation/queue")
    assert "Les cantines devraient être gratuites." in file.text


async def test_le_compteur_de_la_barre_ne_compte_que_les_conversations(
    moderator_client, client
) -> None:
    """Une proposition ne peut plus attendre : la compter aurait donné une pastille qui
    ne mène nulle part, l'écran qui les listait ayant été retiré."""
    from tests.conftest import open_conversation

    _, slug, _ = await open_conversation(moderator_client, title="Publiée")
    await client.post(f"/api/conversations/{slug}/statements", json={"text": "Seule."})
    await propose(client)

    accueil = await moderator_client.get("/moderation/")

    assert "File de modération (1)" in accueil.text


# --- approbation et rejet -----------------------------------------------------------


async def test_approving_publishes_the_conversation_and_its_statements(
    moderator_client, client, session_factory
) -> None:
    await propose(client)
    async with session_factory() as session:
        conversation = await session.scalar(select(Conversation))

    reponse = await moderator_client.post(
        f"/moderation/queue/conversations/{conversation.id}/approve",
        data={"themes": ["institutions"]},
        follow_redirects=False,
    )

    assert reponse.status_code == 303
    detail = await client.get(f"/api/conversations/{conversation.slug}")
    assert detail.status_code == 200
    assert len(detail.json()["statements"]) == 3
    assert detail.json()["state"] == "open"  # approuver, c'est publier


async def test_rejecting_leaves_the_conversation_invisible(
    moderator_client, client, session_factory
) -> None:
    await propose(client)
    async with session_factory() as session:
        conversation = await session.scalar(select(Conversation))

    await moderator_client.post(
        f"/moderation/queue/conversations/{conversation.id}/reject",
        follow_redirects=False,
    )

    assert (await client.get(f"/api/conversations/{conversation.slug}")).status_code == 404
    assert (await client.get("/api/conversations")).json() == []
    assert "Aucune conversation en attente" in (
        await moderator_client.get("/moderation/queue")
    ).text


# --- niveaux et badge ---------------------------------------------------------------


async def test_a_pending_conversation_does_not_count_towards_the_level(client) -> None:
    """Sinon proposer en rafale suffirait à monter de niveau, sans rien publier."""
    await register(client, "nora@exemple.fr")
    await login(client, "nora@exemple.fr")

    await propose(client)

    progression = (await client.get("/api/me")).json()["progress"]
    assert progression["conversations"] == 0
    assert progression["points"] == 0
    assert progression["badges"] == []


async def test_approval_awards_the_badge_and_the_points(
    moderator_client, client, session_factory
) -> None:
    await register(client, "omar@exemple.fr")
    await login(client, "omar@exemple.fr")
    await propose(client)
    async with session_factory() as session:
        conversation = await session.scalar(select(Conversation))

    await moderator_client.post(
        f"/moderation/queue/conversations/{conversation.id}/approve",
        data={"themes": ["institutions"]},
        follow_redirects=False,
    )

    progression = (await client.get("/api/me")).json()["progress"]
    assert progression["conversations"] == 1
    assert progression["points"] == 25
    assert progression["level"] == 2
    assert FIRST_CONVERSATION in [b["code"] for b in progression["badges"]]


async def test_an_account_created_after_proposing_still_gets_the_credit(
    moderator_client, client, session_factory
) -> None:
    """Cas réel : on propose, puis on crée un compte, puis le modérateur approuve.
    Le rattachement doit se faire au moment de l'approbation."""
    await propose(client)
    await register(client, "paula@exemple.fr")
    await login(client, "paula@exemple.fr")

    async with session_factory() as session:
        conversation = await session.scalar(select(Conversation))
        assert conversation.owner_user_id is None

    await moderator_client.post(
        f"/moderation/queue/conversations/{conversation.id}/approve",
        data={"themes": ["institutions"]},
        follow_redirects=False,
    )

    async with session_factory() as session:
        conversation = await session.scalar(select(Conversation))
        proprietaire = await session.get(User, conversation.owner_user_id)
    assert proprietaire.email == "paula@exemple.fr"
    assert (await client.get("/api/me")).json()["progress"]["conversations"] == 1


async def test_a_moderator_creating_a_conversation_also_earns_the_badge(
    moderator_client,
) -> None:
    from tests.conftest import create_conversation

    await create_conversation(moderator_client, title="Créée par la modération")

    progression = (await moderator_client.get("/api/me")).json()["progress"]
    assert progression["conversations"] == 1
    assert FIRST_CONVERSATION in [b["code"] for b in progression["badges"]]
