"""Conversations côté participant, et cycle complet de modération.

Le scénario central : un participant dépose une proposition, elle est INVISIBLE
tant qu'elle n'est pas approuvée, puis elle apparaît. C'est la pré-modération, choisie
comme défaut à l'inverse de Pol.is.
"""

from app.models import Statement
from tests.conftest import create_conversation, login, register


async def open_conversation(moderator_client, *, mode="pre", allow=True, title="Essai"):
    """Crée une conversation, l'ouvre au vote, et renvoie son slug."""
    conversation_id = await create_conversation(moderator_client, title=title, mode=mode)
    data = {
        "title": title,
        "description": "",
        "state": "open",
        "themes": ["institutions"],
        "moderation_mode": mode,
    }
    if allow:
        data["allow_participant_statements"] = "1"
    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}", data=data, follow_redirects=False
    )
    listing = await moderator_client.get("/api/conversations")
    return conversation_id, listing.json()[0]["slug"]


# --- visibilité -----------------------------------------------------------------


async def test_draft_conversation_is_invisible_to_participants(
    moderator_client, client
) -> None:
    await create_conversation(moderator_client, title="Brouillon")

    listing = await client.get("/api/conversations")

    assert listing.json() == []


async def test_open_conversation_is_listed_with_its_approved_statements(
    moderator_client, client
) -> None:
    conversation_id, slug = await open_conversation(moderator_client)
    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}/statements",
        data={"text": "Une proposition d'amorce."},
        follow_redirects=False,
    )

    detail = await client.get(f"/api/conversations/{slug}")

    assert detail.status_code == 200
    assert [s["text"] for s in detail.json()["statements"]] == [
        "Une proposition d'amorce."
    ]


async def test_unknown_slug_is_404(client) -> None:
    assert (await client.get("/api/conversations/inexistante")).status_code == 404


# --- le cycle de publication : directe, sans relecture préalable (MOD-14) --------


async def test_une_proposition_deposee_est_publiee_aussitot(
    moderator_client, client
) -> None:
    """**Le test qui porte le lot.**

    Il n'y a plus de relecture avant publication : une proposition déposée est visible
    immédiatement, et le contrôle se fait après, par le signalement. C'est le scénario de
    recette de C2 retourné — il exigeait l'inverse jusqu'au 15 septembre 2026.
    """
    _, slug = await open_conversation(moderator_client)

    submission = await client.post(
        f"/api/conversations/{slug}/statements",
        json={"text": "Il faut gratuité totale des transports."},
    )

    assert submission.status_code == 201
    assert submission.json()["moderation_status"] == "approved"
    assert submission.json()["visible"] is True
    # Visible tout de suite, sans qu'aucun modérateur soit passé.
    statements = (await client.get(f"/api/conversations/{slug}")).json()["statements"]
    assert [s["text"] for s in statements] == ["Il faut gratuité totale des transports."]


async def test_aucune_proposition_n_arrive_plus_dans_la_file(
    moderator_client, client
) -> None:
    """La section « Propositions en attente » a été retirée avec la pré-modération : plus
    rien ne peut y arriver, et l'écran ne doit donc plus la montrer."""
    _, slug = await open_conversation(moderator_client)
    await client.post(
        f"/api/conversations/{slug}/statements", json={"text": "Publiée d'emblée."}
    )

    queue = await moderator_client.get("/moderation/queue")

    assert "Propositions en attente" not in queue.text
    assert "Publiée d'emblée." not in queue.text
    assert "Rien à modérer" in queue.text


async def test_le_geste_d_approbation_d_une_proposition_n_existe_plus(
    moderator_client, client
) -> None:
    """Les deux boutons « approuver / rejeter sans grille » sont partis avec la file.
    La route aussi : la laisser répondre aurait gardé un chemin par lequel une
    proposition publiée pouvait être rejetée sans passer par le signalement."""
    _, slug = await open_conversation(moderator_client)
    submission = await client.post(
        f"/api/conversations/{slug}/statements", json={"text": "Déjà en ligne."}
    )

    reponse = await moderator_client.post(
        f"/moderation/statements/{submission.json()['id']}/reject",
        follow_redirects=False,
    )

    assert reponse.status_code == 404
    # Et elle est toujours en ligne.
    statements = (await client.get(f"/api/conversations/{slug}")).json()["statements"]
    assert [s["text"] for s in statements] == ["Déjà en ligne."]


# --- qui peut proposer ----------------------------------------------------------


async def test_an_anonymous_visitor_can_propose(moderator_client, client) -> None:
    """Même contrat que le futur endpoint de vote : aucun compte n'est exigé."""
    _, slug = await open_conversation(moderator_client)

    response = await client.post(
        f"/api/conversations/{slug}/statements", json={"text": "Proposée en anonyme."}
    )

    assert response.status_code == 201


async def test_an_UNVERIFIED_account_can_propose(moderator_client, client) -> None:
    _, slug = await open_conversation(moderator_client)
    await register(client, "lea@exemple.fr")
    await login(client, "lea@exemple.fr")
    assert (await client.get("/api/me")).json()["account"]["is_verified"] is False

    response = await client.post(
        f"/api/conversations/{slug}/statements", json={"text": "Proposée sans confirmer."}
    )

    assert response.status_code == 201


async def test_the_author_is_recorded_on_the_proposal(
    moderator_client, client, session_factory
) -> None:
    _, slug = await open_conversation(moderator_client)
    participant_id = (await client.get("/api/me")).json()["participant_id"]

    reponse = await client.post(
        f"/api/conversations/{slug}/statements", json={"text": "Signée."}
    )

    # Plus de file où le lire depuis le MOD-14 : c'est la proposition qui porte son
    # auteur, et c'est ce qui compte — le rappel (MOD-6) et la contestation en dépendent.
    async with session_factory() as session:
        statement = await session.get(Statement, reponse.json()["id"])
        assert statement.author_participant_id == participant_id


# --- refus ----------------------------------------------------------------------


async def test_a_closed_conversation_refuses_new_statements(
    moderator_client, client
) -> None:
    conversation_id, slug = await open_conversation(moderator_client)
    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}",
        data={
            "title": "Essai",
            "description": "",
            "state": "closed",
            "themes": ["institutions"],
            "moderation_mode": "pre",
            "allow_participant_statements": "1",
        },
        follow_redirects=False,
    )

    response = await client.post(
        f"/api/conversations/{slug}/statements", json={"text": "Trop tard."}
    )

    assert response.status_code == 409


async def test_a_conversation_that_forbids_statements_refuses_them(
    moderator_client, client
) -> None:
    _, slug = await open_conversation(moderator_client, allow=False)

    response = await client.post(
        f"/api/conversations/{slug}/statements", json={"text": "Non prévue."}
    )

    assert response.status_code == 409


async def test_an_empty_or_oversized_statement_is_refused(
    moderator_client, client
) -> None:
    _, slug = await open_conversation(moderator_client)

    empty = await client.post(f"/api/conversations/{slug}/statements", json={"text": "   "})
    huge = await client.post(
        f"/api/conversations/{slug}/statements", json={"text": "a" * 501}
    )

    assert empty.status_code == 422
    assert huge.status_code == 422
