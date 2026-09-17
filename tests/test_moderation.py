"""Modération : accès réservé, création de conversations, file d'attente.

Le circuit fonctionnel de Pol.is disparaissant, ces écrans sont désormais le seul
moyen de créer et de modérer. Ils doivent donc être à la fois utilisables et fermés.
"""

from sqlalchemy import select

from app.models import Conversation, ModerationStatus, Participant, Statement, User
from app.services.accounts import ensure_superuser
from tests.conftest import (
    MODERATOR_EMAIL,
    PASSWORD,
    create_conversation,
    login,
    open_conversation,
    register,
)


# --- CLI de création du modérateur ----------------------------------------------


async def test_ensure_superuser_creates_a_moderator(session_factory) -> None:
    async with session_factory() as session:
        user, action = await ensure_superuser(session, "chef@exemple.fr", PASSWORD)

    assert action == "created"
    assert user.is_superuser is True
    # Créé en local par un administrateur : aucune confirmation d'e-mail à attendre.
    assert user.is_verified is True


async def test_ensure_superuser_promotes_an_existing_account(
    client, session_factory
) -> None:
    await register(client, "simple@exemple.fr")

    async with session_factory() as session:
        user, action = await ensure_superuser(session, "simple@exemple.fr", PASSWORD)

    assert action == "promoted"
    assert user.is_superuser is True


async def test_ensure_superuser_is_idempotent(session_factory) -> None:
    async with session_factory() as session:
        await ensure_superuser(session, "chef@exemple.fr", PASSWORD)
        _, action = await ensure_superuser(session, "chef@exemple.fr", PASSWORD)

    assert action == "unchanged"


async def test_without_the_flag_an_existing_password_is_left_alone(
    client_factory, session_factory
) -> None:
    """Le piège à éviter : relancer la commande ne doit pas changer le mot de passe
    d'un compte en service sans qu'on l'ait demandé."""
    async with session_factory() as session:
        await ensure_superuser(session, "stable@exemple.fr", PASSWORD)
        _, action = await ensure_superuser(session, "stable@exemple.fr", "AutreMotDePasse!9")

    assert action == "unchanged"
    async with client_factory() as client:
        assert (await login(client, "stable@exemple.fr", PASSWORD)).status_code == 204


async def test_reset_password_changes_the_password_without_deleting_the_account(
    client_factory, session_factory
) -> None:
    """Le cas d'usage : un mot de passe a fuité. Supprimer le compte emporterait son
    participant, donc son historique de votes ; il faut pouvoir le changer sur place."""
    async with session_factory() as session:
        user, _ = await ensure_superuser(session, "fuite@exemple.fr", PASSWORD)
        original_id = user.id
        changed, action = await ensure_superuser(
            session, "fuite@exemple.fr", "MotDePasseTournE!2027", reset_password=True
        )

    assert action == "password_reset"
    assert changed.id == original_id  # même compte, donc même participant
    async with client_factory() as client:
        assert (await login(client, "fuite@exemple.fr", PASSWORD)).status_code == 400
        assert (
            await login(client, "fuite@exemple.fr", "MotDePasseTournE!2027")
        ).status_code == 204


async def test_reset_password_also_promotes_an_ordinary_account(
    client, session_factory
) -> None:
    await register(client, "double@exemple.fr")

    async with session_factory() as session:
        user, action = await ensure_superuser(
            session, "double@exemple.fr", "NouveauSecret!2027", reset_password=True
        )

    assert action == "password_reset"
    assert user.is_superuser is True


# --- fermeture des écrans -------------------------------------------------------


async def test_moderation_is_closed_to_anonymous_visitors(client) -> None:
    response = await client.get("/moderation/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/moderation/login"


async def test_moderation_is_closed_to_ordinary_accounts(client) -> None:
    """Avoir un compte ne suffit pas : il faut être is_superuser."""
    await register(client, "curieux@exemple.fr")
    await login(client, "curieux@exemple.fr")

    response = await client.get("/moderation/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/moderation/login"


async def test_admin_is_closed_to_anonymous_visitors(client) -> None:
    response = await client.get("/admin/", follow_redirects=False)

    assert response.status_code == 302
    assert "/moderation/login" in response.headers["location"]


async def test_login_with_a_non_moderator_account_is_refused(client) -> None:
    await register(client, "curieux@exemple.fr")

    response = await client.post(
        "/moderation/login",
        data={"username": "curieux@exemple.fr", "password": PASSWORD},
        follow_redirects=False,
    )

    assert response.status_code == 401


async def test_moderator_reaches_the_screens(moderator_client) -> None:
    index = await moderator_client.get("/moderation/")
    queue = await moderator_client.get("/moderation/queue")
    admin = await moderator_client.get("/admin/", follow_redirects=False)

    assert index.status_code == 200
    assert queue.status_code == 200
    assert "Rien à modérer" in queue.text
    # Le même cookie ouvre l'admin générique : une seule connexion pour les deux.
    assert admin.status_code == 200


# --- création et édition --------------------------------------------------------


async def test_moderator_creates_a_conversation_with_a_readable_slug(
    moderator_client, session_factory
) -> None:
    conversation_id = await create_conversation(
        moderator_client, title="Les transports en 2027"
    )

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)

    assert conversation.slug == "les-transports-en-2027"
    assert conversation.state.value == "draft"  # rien n'est publié par accident


async def test_two_conversations_with_the_same_title_get_distinct_slugs(
    moderator_client, session_factory
) -> None:
    first = await create_conversation(moderator_client, title="Même titre")
    second = await create_conversation(moderator_client, title="Même titre")

    async with session_factory() as session:
        a = await session.get(Conversation, first)
        b = await session.get(Conversation, second)

    assert a.slug != b.slug
    assert b.slug.startswith("meme-titre-")


async def test_seed_statements_are_approved_immediately(
    moderator_client, session_factory
) -> None:
    conversation_id = await create_conversation(moderator_client)

    response = await moderator_client.post(
        f"/moderation/conversations/{conversation_id}/statements",
        data={"text": "Le train doit être moins cher que l'avion."},
        follow_redirects=False,
    )
    assert response.status_code == 303

    async with session_factory() as session:
        statement = await session.scalar(select(Statement))

    assert statement.is_seed is True
    assert statement.moderation_status is ModerationStatus.approved


async def test_moderator_can_open_a_conversation(
    moderator_client, session_factory
) -> None:
    conversation_id = await create_conversation(moderator_client)

    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}",
        data={
            "title": "Titre revu",
            "description": "Une description",
            "state": "open",
            "themes": ["institutions"],
            "moderation_mode": "pre",
            "allow_participant_statements": "1",
        },
        follow_redirects=False,
    )

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)

    assert conversation.state.value == "open"
    assert conversation.title == "Titre revu"
    assert conversation.allow_participant_statements is True


async def test_unchecked_box_disables_participant_statements(
    moderator_client, session_factory
) -> None:
    """Une case décochée n'est pas envoyée par le navigateur : le champ absent doit
    bien valoir False, et non « inchangé »."""
    conversation_id = await create_conversation(moderator_client)

    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}",
        data={
            "title": "T",
            "description": "",
            "state": "open",
            "themes": ["institutions"],
            "moderation_mode": "pre",
        },
        follow_redirects=False,
    )

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)

    assert conversation.allow_participant_statements is False


async def test_moderator_account_is_recorded_as_owner(
    moderator_client, session_factory, moderator
) -> None:
    conversation_id = await create_conversation(moderator_client)

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        owner = await session.get(User, conversation.owner_user_id)

    assert owner.email == MODERATOR_EMAIL


# --- ce que l'admin générique laisse modifier -------------------------------------


async def test_the_admin_edit_forms_expose_only_what_is_meant_to_be_edited(
    moderator_client, session_factory
) -> None:
    """Sans `form_columns`, sqladmin bâtit son formulaire sur TOUTES les colonnes.

    `is_meta` devenait ainsi une case à cocher sans intitulé explicite dans
    /admin/statement/edit — alors qu'elle multiplie par ~10 la priorité de routage
    d'une proposition. Personne n'a demandé ce réglage : il doit être inatteignable
    tant qu'il n'est pas exposé délibérément.
    """
    _, _, statements = await open_conversation(moderator_client, title="Admin borne")

    async with session_factory() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.title == "Admin borne")
        )
        compte = await session.scalar(select(User))

    proposition = await moderator_client.get(f"/admin/statement/edit/{statements[0]}")
    espace = await moderator_client.get(f"/admin/conversation/edit/{conversation.id}")
    utilisateur = await moderator_client.get(f"/admin/user/edit/{compte.id}")

    # Le formulaire reste utile : corriger une faute, trancher une modération.
    assert 'name="text"' in proposition.text
    assert 'name="moderation_status"' in proposition.text
    # Mais les leviers du pipeline ne s'attrapent plus par accident.
    assert 'name="is_meta"' not in proposition.text
    assert 'name="is_seed"' not in proposition.text

    assert 'name="title"' in espace.text
    # Le slug est l'URL publique : la changer casserait les liens déjà partagés.
    assert 'name="slug"' not in espace.text
    assert 'name="proposed_by_participant_id"' not in espace.text

    # `is_active` reste éditable : c'est le levier d'éjection d'un compte.
    assert 'name="is_active"' in utilisateur.text
    assert 'name="hashed_password"' not in utilisateur.text


async def test_a_participant_row_cannot_be_edited_by_hand(
    moderator_client, client, session_factory
) -> None:
    """`anon_token` est un secret de session : aucun formulaire ne doit l'afficher."""
    async with session_factory() as session:
        participant = Participant(anon_token="jeton-a-ne-pas-editer")
        session.add(participant)
        await session.commit()
        await session.refresh(participant)

    reponse = await moderator_client.get(
        f"/admin/participant/edit/{participant.id}", follow_redirects=False
    )

    assert reponse.status_code in (302, 403, 404)
    liste = await moderator_client.get("/admin/participant/list")
    assert liste.status_code == 200


# --- seuil de participation par conversation --------------------------------------


async def test_a_moderator_can_set_the_vote_threshold_from_the_editor(
    moderator_client, session_factory
) -> None:
    """Même principe que force_group_count : vide = automatique, valeur = imposée."""
    conversation_id, _, _ = await open_conversation(
        moderator_client, title="Seuil réglable", statements=8
    )

    page = await moderator_client.get(f"/moderation/conversations/{conversation_id}")
    # Le champ annonce ce que vaut l'automatique : 8 propositions -> 5 votes.
    assert 'name="min_user_votes"' in page.text
    assert "Automatique — 5 vote(s) pour 8 proposition(s)" in page.text

    reglages = {
        "title": "Seuil réglable",
        "description": "",
        "state": "open",
        "themes": ["institutions"],
        "moderation_mode": "pre",
        "allow_participant_statements": "1",
        "force_group_count": "",
    }
    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}",
        data={**reglages, "min_user_votes": "3"},
        follow_redirects=False,
    )
    async with session_factory() as session:
        impose = (await session.get(Conversation, conversation_id)).min_user_votes

    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}",
        data={**reglages, "min_user_votes": ""},
        follow_redirects=False,
    )
    async with session_factory() as session:
        automatique = (await session.get(Conversation, conversation_id)).min_user_votes

    assert impose == 3
    assert automatique is None


async def test_a_nonsense_setting_falls_back_to_automatic_instead_of_500(
    moderator_client, session_factory
) -> None:
    """Un POST forgé à la main faisait tomber `int()` — donc une erreur 500."""
    conversation_id, _, _ = await open_conversation(moderator_client, title="Réglage sale")

    reponse = await moderator_client.post(
        f"/moderation/conversations/{conversation_id}",
        data={
            "title": "Réglage sale",
            "description": "",
            "state": "open",
            "themes": ["institutions"],
            "moderation_mode": "pre",
            "force_group_count": "abc",
            "min_user_votes": "-4",
        },
        follow_redirects=False,
    )

    assert reponse.status_code == 303
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
    assert conversation.force_group_count is None
    assert conversation.min_user_votes is None
