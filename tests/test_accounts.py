"""Comptes : inscription, connexion, réinitialisation, vérification."""

from httpx import AsyncClient

from app.auth.users import REQUIRE_VERIFICATION
from tests.conftest import PASSWORD, login, register, token_from


async def test_register_creates_unverified_account(client: AsyncClient) -> None:
    response = await register(client, "alice@exemple.fr")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["email"] == "alice@exemple.fr"
    assert body["is_active"] is True
    assert body["is_verified"] is False  # peuplé dès le départ, à False
    assert "password" not in body and "hashed_password" not in body


async def test_register_sends_one_verification_email(client: AsyncClient, outbox) -> None:
    await register(client, "bob@exemple.fr")

    assert len(outbox) == 1
    assert outbox[0]["To"] == "bob@exemple.fr"
    assert "token=" in outbox[0].get_content()


async def test_password_is_hashed_not_stored_in_clear(
    client: AsyncClient, session_factory
) -> None:
    from sqlalchemy import select

    from app.models import User

    await register(client, "carole@exemple.fr")

    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.email == "carole@exemple.fr"))

    assert user is not None
    assert PASSWORD not in user.hashed_password
    assert user.hashed_password.startswith("$argon2")


async def test_login_with_wrong_password_is_rejected(client: AsyncClient) -> None:
    await register(client, "david@exemple.fr")

    response = await login(client, "david@exemple.fr", "mauvais-mot-de-passe")

    assert response.status_code == 400


async def test_login_then_users_me(client: AsyncClient) -> None:
    await register(client, "emma@exemple.fr")

    assert (await login(client, "emma@exemple.fr")).status_code == 204

    response = await client.get("/users/me")
    assert response.status_code == 200
    assert response.json()["email"] == "emma@exemple.fr"


async def test_users_me_requires_a_session(client: AsyncClient) -> None:
    assert (await client.get("/users/me")).status_code == 401


async def test_password_reset_cycle(client_factory, outbox) -> None:
    """Cycle complet : oubli -> e-mail -> nouveau mot de passe -> connexion."""
    async with client_factory() as client:
        await register(client, "fanny@exemple.fr")
        outbox.clear()  # écarte l'e-mail de vérification

        forgot = await client.post(
            "/auth/forgot-password", json={"email": "fanny@exemple.fr"}
        )
        assert forgot.status_code == 202
        assert len(outbox) == 1

        token = token_from(outbox[0])
        reset = await client.post(
            "/auth/reset-password",
            json={"token": token, "password": "NouveauMotDePasse!2027"},
        )
        assert reset.status_code == 200, reset.text

    # L'ancien mot de passe ne marche plus, le nouveau marche.
    async with client_factory() as fresh:
        assert (await login(fresh, "fanny@exemple.fr", PASSWORD)).status_code == 400
        assert (
            await login(fresh, "fanny@exemple.fr", "NouveauMotDePasse!2027")
        ).status_code == 204


async def test_forgot_password_on_unknown_address_does_not_leak(
    client: AsyncClient, outbox
) -> None:
    """Répondre 202 sans envoyer : une adresse inconnue ne doit pas être révélée."""
    response = await client.post(
        "/auth/forgot-password", json={"email": "inconnu@exemple.fr"}
    )

    assert response.status_code == 202
    assert outbox == []


async def test_verification_marks_account_verified(client: AsyncClient, outbox) -> None:
    await register(client, "gaspard@exemple.fr")
    token = token_from(outbox[0])

    response = await client.post("/auth/verify", json={"token": token})

    assert response.status_code == 200, response.text
    assert response.json()["is_verified"] is True


async def test_verification_stays_optional() -> None:
    """Garde-fou de cadrage.

    Si ce drapeau passe à True, la vérification devient obligatoire pour voter et
    tout participant non confirmé est bloqué. C'est une décision de produit, pas un
    détail technique : elle doit être prise sciemment (cf. CHANTIER_C_LOG.md).
    """
    assert REQUIRE_VERIFICATION is False


# --- reprise en main d'un compte compromis ----------------------------------------


async def test_changing_the_password_closes_the_sessions_already_open(
    client_factory, outbox
) -> None:
    """Le cœur du problème : un JWT restait valable jusqu'à son expiration.

    Une personne dont la session a été volée pouvait changer son mot de passe sans
    éjecter l'intrus — le jeton ne portait que `sub`, `aud` et `exp`. Il porte
    désormais l'empreinte du hachage : la comparaison faite à chaque lecture fait
    tomber toutes les sessions dès que le mot de passe change.
    """
    async with client_factory() as intrus, client_factory() as victime:
        await register(intrus, "volee@exemple.fr")
        await login(intrus, "volee@exemple.fr")
        # Session ouverte, et bien vivante avant le changement.
        assert (await intrus.get("/users/me")).status_code == 200

        outbox.clear()
        await victime.post("/auth/forgot-password", json={"email": "volee@exemple.fr"})
        await victime.post(
            "/auth/reset-password",
            json={"token": token_from(outbox[0]), "password": "AutreMotDePasse!2027"},
        )

        # Même cookie, même jeton : il ne vaut plus rien.
        assert (await intrus.get("/users/me")).status_code == 401

    # Et la personne légitime reprend la main normalement.
    async with client_factory() as fresh:
        assert (
            await login(fresh, "volee@exemple.fr", "AutreMotDePasse!2027")
        ).status_code == 204
        assert (await fresh.get("/users/me")).status_code == 200


async def test_deactivating_an_account_closes_its_session_immediately(
    client_factory, session_factory
) -> None:
    """L'autre levier, sans rien demander à la personne visée.

    Décocher `is_active` depuis /admin coupe l'accès à la requête suivante, sans
    redémarrage : c'est ce qui permet d'éjecter un compte abusif dont on ne veut pas
    (ou ne peut pas) changer le mot de passe.
    """
    from sqlalchemy import select

    from app.models import User

    async with client_factory() as client:
        await register(client, "abusif@exemple.fr")
        await login(client, "abusif@exemple.fr")
        assert (await client.get("/users/me")).status_code == 200

        async with session_factory() as session:
            compte = await session.scalar(
                select(User).where(User.email == "abusif@exemple.fr")
            )
            compte.is_active = False
            await session.commit()

        assert (await client.get("/users/me")).status_code == 401
        # Et l'identité anonyme reprend le dessus, sans page en erreur.
        assert (await client.get("/api/me")).json()["account"] is None


async def test_an_ordinary_session_survives_its_own_requests(client) -> None:
    """Garde-fou : l'empreinte ne doit pas invalider une session normale.

    Le hachage argon2 est salé, donc recalculé il différerait — c'est bien
    l'empreinte du hachage STOCKÉ qui est comparée, pas un nouveau hachage.
    """
    await register(client, "ordinaire@exemple.fr")
    await login(client, "ordinaire@exemple.fr")

    for _ in range(3):
        assert (await client.get("/users/me")).status_code == 200


# --- plafond des tentatives de connexion ------------------------------------------


async def test_login_attempts_are_capped_per_ip(client) -> None:
    """/admin et /moderation restent publics : le mot de passe est la seule barrière.

    Sans plafond, rien n'empêche de l'essayer en boucle — et chaque essai coûte au
    serveur un hachage argon2id.
    """
    from app.services.rate_limit import LOGIN_PER_IP_BURST

    await register(client, "cible@exemple.fr")

    codes = []
    for _ in range(LOGIN_PER_IP_BURST + 2):
        reponse = await client.post(
            "/auth/login",
            data={"username": "cible@exemple.fr", "password": "MauvaisMotDePasse!1"},
        )
        codes.append(reponse.status_code)

    assert codes[:LOGIN_PER_IP_BURST] == [400] * LOGIN_PER_IP_BURST
    assert codes[-1] == 429
    # Le client sait quand revenir.
    derniere = await client.post(
        "/auth/login",
        data={"username": "cible@exemple.fr", "password": "MauvaisMotDePasse!1"},
    )
    assert derniere.headers["retry-after"] == "60"


async def test_the_cap_also_covers_the_moderation_login_page(client) -> None:
    """Ne plafonner que /moderation/login se contournerait en changeant d'URL :
    /compte/connexion ouvre le même cookie de session, donc /admin par ricochet."""
    from app.services.rate_limit import LOGIN_PER_IP_BURST

    for _ in range(LOGIN_PER_IP_BURST):
        await client.post(
            "/compte/connexion",
            data={"username": "inconnu@exemple.fr", "password": "x"},
        )

    bloque = await client.post(
        "/moderation/login",
        data={"username": "inconnu@exemple.fr", "password": "x"},
    )

    assert bloque.status_code == 429
    assert "Trop de tentatives" in bloque.text


async def test_the_cap_does_not_lock_out_the_targeted_account(client_factory) -> None:
    """Le plafond porte sur l'IP, jamais sur le compte visé.

    Un plafond par compte permettrait à n'importe qui d'enfermer dehors le seul
    modérateur du site en brûlant son quota avec de mauvais mots de passe. Ce test
    fige ce choix : le seau est nominatif de l'adresse, pas de la victime.
    """
    from app.services.rate_limit import LOGIN_PER_IP_BURST

    async with client_factory() as attaquant:
        await register(attaquant, "victime@exemple.fr")
        for _ in range(LOGIN_PER_IP_BURST + 2):
            await attaquant.post(
                "/auth/login",
                data={"username": "victime@exemple.fr", "password": "faux"},
            )

    # Même adresse IP dans les tests : on vérifie donc l'autre moitié de la règle —
    # le compte visé n'est ni verrouillé ni marqué, seul le seau de l'IP l'est.
    from sqlalchemy import select

    from app.models import User
    from tests.conftest import _SESSION_FACTORY

    async with _SESSION_FACTORY[0]() as session:
        compte = await session.scalar(
            select(User).where(User.email == "victime@exemple.fr")
        )

    assert compte.is_active is True


# --- suppression de ses données ---------------------------------------------------


async def test_an_account_can_delete_itself_with_its_password(
    moderator_client, client_factory, session_factory
) -> None:
    """Droit à l'effacement : jusqu'ici, une demande se traitait en SQL à la main."""
    from sqlalchemy import func, select

    from app.models import Participant, Statement, User, Vote
    from tests.conftest import open_conversation

    _, slug, statements = await open_conversation(moderator_client, title="Effacement")

    async with client_factory() as navigateur:
        await register(navigateur, "partant@exemple.fr")
        await login(navigateur, "partant@exemple.fr")
        await navigateur.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statements[0], "value": 1},
        )
        await navigateur.post(
            f"/api/conversations/{slug}/statements",
            json={"text": "Une proposition qui restera en ligne."},
        )

        # Un cookie volé ne doit pas suffire à détruire l'historique de quelqu'un.
        refuse = await navigateur.post(
            "/compte/suppression", data={"password": "PasLeBonMotDePasse!9"}
        )
        assert "Mot de passe incorrect" in refuse.text

        async with session_factory() as session:
            assert await session.scalar(select(func.count(User.id))) == 2  # + modérateur

        fait = await navigateur.post("/compte/suppression", data={"password": PASSWORD})

    assert "supprimées" in fait.text

    async with session_factory() as session:
        comptes = list(await session.scalars(select(User.email)))
        # Le modérateur garde le sien : on compte donc les participants restants,
        # pas « zéro ».
        participants = await session.scalar(select(func.count(Participant.id)))
        votes = await session.scalar(select(func.count(Vote.id)))
        proposition = await session.scalar(
            select(Statement).where(
                Statement.text == "Une proposition qui restera en ligne."
            )
        )

    assert "partant@exemple.fr" not in comptes
    # 1 = celui du modérateur. Le participant du compte supprimé doit avoir disparu,
    # pas être resté détaché : c'est lui qui porte les votes.
    assert participants == 1
    assert votes == 0
    # Le texte publié reste — d'autres ont pu voter dessus — mais détaché de l'auteur.
    assert proposition is not None
    assert proposition.author_participant_id is None


async def test_a_participant_without_an_account_can_erase_their_data(
    moderator_client, client, session_factory
) -> None:
    """La majorité des participants n'a pas de compte : sans cette route, le droit à
    l'effacement leur serait inapplicable. Le cookie signé fait preuve de possession."""
    from sqlalchemy import func, select

    from app.models import Participant, Vote
    from tests.conftest import open_conversation

    _, slug, statements = await open_conversation(moderator_client, title="Anonyme")

    vote = await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": statements[0], "value": -1},
    )
    anonyme = vote.json()["participant_id"]

    # Sans la case cochée, rien ne bouge.
    await client.post("/compte/suppression", data={})
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Vote.id))) == 1

    fait = await client.post("/compte/suppression", data={"confirmation": "1"})

    assert "supprimées" in fait.text
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Vote.id))) == 0
        assert await session.get(Participant, anonyme) is None


async def test_the_deletion_page_never_creates_a_participant(client, session_factory) -> None:
    """Ouvrir la page d'effacement fabriquerait la donnée qu'on vient effacer."""
    from sqlalchemy import func, select

    from app.models import Participant

    page = await client.get("/compte/suppression")

    assert page.status_code == 200
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Participant.id))) == 0


async def test_deleting_an_account_detaches_the_conversations_it_proposed(
    moderator_client, client_factory, session_factory
) -> None:
    """Symétrique du cas proposition, et vérifié parce que l'attribution d'une
    conversation a DÉJÀ été un bug une fois (revue des situations limites : elle ne
    suivait pas le participant lors d'une fusion).

    Deux colonnes sont en jeu ici, pas une : `proposed_by_participant_id` (l'auteur) et
    `owner_user_id` (le compte, renseigné à l'approbation). Les deux doivent tomber à
    NULL, et la conversation rester en ligne — d'autres personnes y ont voté.
    """
    from sqlalchemy import func, select

    from app.models import Conversation, ConversationState, ModerationStatus, Statement

    async with client_factory() as auteur:
        await register(auteur, "proposeur@exemple.fr")
        await login(auteur, "proposeur@exemple.fr")
        await auteur.post(
            "/proposer",
            data={
                "title": "Conversation dont l'auteur partira",
                "description": "",
                "statements": ["Une.", "Deux.", "Trois."],
            },
            follow_redirects=True,
        )

        async with session_factory() as session:
            conversation = await session.scalar(
                select(Conversation).where(
                    Conversation.title == "Conversation dont l'auteur partira"
                )
            )
        assert conversation.proposed_by_participant_id is not None

        # Approuvée : c'est à ce moment que `owner_user_id` est renseigné.
        await moderator_client.post(
            f"/moderation/queue/conversations/{conversation.id}/approve",
            data={"themes": ["institutions"]},
            follow_redirects=False,
        )
        async with session_factory() as session:
            approuvee = await session.get(Conversation, conversation.id)
        assert approuvee.owner_user_id is not None

        await auteur.post("/compte/suppression", data={"password": PASSWORD})

    async with session_factory() as session:
        restante = await session.get(Conversation, conversation.id)
        propositions = await session.scalar(
            select(func.count(Statement.id)).where(
                Statement.conversation_id == conversation.id
            )
        )

    # La conversation survit, publiée, mais plus rien ne la relie à quelqu'un.
    assert restante is not None
    assert restante.proposed_by_participant_id is None
    assert restante.owner_user_id is None
    assert restante.state is ConversationState.open
    assert restante.moderation_status is ModerationStatus.approved
    assert propositions == 3


async def test_a_pending_conversation_survives_its_author_leaving(
    moderator_client, client_factory, session_factory
) -> None:
    """Cas limite du précédent : le départ a lieu AVANT la modération.

    `moderate_conversation` va chercher le participant proposant pour rattacher la
    conversation à son compte, et `on_conversation_approved` décerne un badge à son
    propriétaire. Les deux doivent encaisser une ligne disparue sans tomber — c'est
    exactement le genre de chemin qui rendait 500 avant la revue des situations limites.
    """
    from sqlalchemy import select

    from app.models import Conversation, ConversationState

    async with client_factory() as auteur:
        await register(auteur, "partant-avant@exemple.fr")
        await login(auteur, "partant-avant@exemple.fr")
        await auteur.post(
            "/proposer",
            data={
                "title": "Proposition orpheline",
                "description": "",
                "statements": ["Une.", "Deux.", "Trois."],
            },
            follow_redirects=True,
        )
        await auteur.post("/compte/suppression", data={"password": PASSWORD})

    async with session_factory() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.title == "Proposition orpheline")
        )
    assert conversation.proposed_by_participant_id is None

    decision = await moderator_client.post(
        f"/moderation/queue/conversations/{conversation.id}/approve",
        data={"themes": ["institutions"]},
        follow_redirects=False,
    )

    assert decision.status_code == 303
    async with session_factory() as session:
        approuvee = await session.get(Conversation, conversation.id)
    assert approuvee.state is ConversationState.open
    # Aucun badge n'est décerné à personne : le compte n'existe plus.
    assert approuvee.owner_user_id is None


async def test_a_malformed_address_gets_a_message_not_a_500(client):
    """Une faute de frappe dans l'adresse ne doit pas casser la page.

    `type="email"` du navigateur accepte des adresses que le validateur du serveur
    refuse — un domaine réservé comme `.invalid` en est une. Avant le correctif, ce
    cas remontait une ValidationError brute, donc une 500 sur le formulaire
    d'inscription. Régression trouvée en vérifiant une mise en production, pas par
    un test : d'où celui-ci.
    """
    response = await client.post(
        "/compte/inscription",
        data={"username": "faute-de-frappe@exemple.invalid", "password": "MotDePasse!2027"},
    )
    assert response.status_code == 200, response.status_code
    # Sans apostrophe dans le motif : Jinja échappe l'apostrophe en `&#39;`, et une
    # assertion qui en contient une échouerait sur un message pourtant présent.
    assert "pas valide" in response.text
    # L'adresse saisie est réaffichée, et le champ fautif est désigné.
    assert 'class="invalide"' in response.text
    assert "faute-de-frappe@exemple.invalid" in response.text


# --- profil : avatar, bio, nom affiché --------------------------------------------


async def test_the_account_page_falls_back_to_the_email_without_a_display_name(
    client,
) -> None:
    """`display_name` existait déjà en base sans jamais être montré. Sans lui,
    l'e-mail reste l'identité affichée — c'est le repli, pas une régression."""
    await register(client, "sansnom@exemple.fr")
    await login(client, "sansnom@exemple.fr")

    page = (await client.get("/compte")).text
    assert "sansnom@exemple.fr" in page


async def test_setting_a_display_name_and_bio_replaces_the_email_on_screen(
    client,
) -> None:
    await register(client, "avecnom@exemple.fr")
    await login(client, "avecnom@exemple.fr")

    reponse = await client.post(
        "/compte/profil",
        data={"display_name": "Alix", "bio": "Ici pour comprendre les débats."},
        follow_redirects=False,
    )
    assert reponse.status_code == 303, reponse.text

    page = (await client.get("/compte")).text
    assert "Alix" in page
    assert "Ici pour comprendre les débats." in page
    # L'e-mail reste affiché en second, pour que la personne se reconnaisse.
    assert "avecnom@exemple.fr" in page

    en_tete = (await client.get("/")).text
    assert "Alix" in en_tete
    assert "avecnom@exemple.fr" not in en_tete


async def test_an_overly_long_display_name_is_refused_without_a_500(client) -> None:
    """Refusé par l'écran, pas par la base : une VARCHAR(120) dépassée par une
    saisie que l'écran aurait dû arrêter serait une 500, pas un message."""
    await register(client, "tropnom@exemple.fr")
    await login(client, "tropnom@exemple.fr")

    reponse = await client.post(
        "/compte/profil",
        data={"display_name": "x" * 41, "bio": ""},
    )
    assert reponse.status_code == 200
    assert "dépasser" in reponse.text
    assert 'class="invalide"' in reponse.text


async def test_clearing_the_display_name_falls_back_to_the_email_again(client) -> None:
    await register(client, "retourarriere@exemple.fr")
    await login(client, "retourarriere@exemple.fr")

    await client.post("/compte/profil", data={"display_name": "Un nom", "bio": ""})
    await client.post("/compte/profil", data={"display_name": "", "bio": ""})

    page = (await client.get("/compte")).text
    assert "retourarriere@exemple.fr" in page
    assert "Un nom" not in page


async def test_the_account_page_shows_a_deterministic_avatar(client) -> None:
    """Le même dessin d'un chargement à l'autre : ce n'est pas un tirage à
    l'affichage, mais un calcul sur l'identifiant du compte."""
    import re

    await register(client, "avatar@exemple.fr")
    await login(client, "avatar@exemple.fr")

    premiere = (await client.get("/compte")).text
    seconde = (await client.get("/compte")).text

    motif = re.search(r'<svg class="identicon[^"]*".*?</svg>', premiere, re.S)
    assert motif is not None, "aucun identicon sur la page de compte"
    assert motif.group(0) in seconde


async def test_logging_out_from_the_account_page_ends_the_session(client):
    """La déconnexion vit dans la page de compte, et elle ferme bien la session.

    Ajouté au chantier F4, qui a déplacé le bouton de l'en-tête vers la page de
    compte : l'en-tête d'un téléphone n'a pas la place d'un bouton de plus, et c'est
    dans la page de compte qu'on va chercher à se déconnecter. Rien ne couvrait ce
    chemin — le déplacer sans ce test revenait à parier qu'il marchait encore.
    """
    await register(client, "deconnexion@exemple.fr")
    await login(client, "deconnexion@exemple.fr")

    page = (await client.get("/compte")).text
    assert 'action="/compte/deconnexion"' in page, "le formulaire a disparu de la page"

    reponse = await client.post("/compte/deconnexion", data={}, follow_redirects=False)
    assert reponse.status_code == 303, reponse.text

    # La page de compte n'est plus accessible : elle renvoie vers la connexion.
    apres = await client.get("/compte", follow_redirects=False)
    assert apres.status_code == 303
    assert apres.headers["location"].startswith("/compte/connexion")
