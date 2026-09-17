"""Interface participant : pages, parcours de vote, et circuit du lien e-mail."""

from httpx import AsyncClient

from tests.conftest import PASSWORD, login, open_conversation, register, token_from


def link_in(message) -> str:
    for word in message.get_content().split():
        if "token=" in word:
            return word
    raise AssertionError("aucun lien dans l'e-mail")


# --- pages -----------------------------------------------------------------------


async def test_home_lists_open_conversations_only(moderator_client, client) -> None:
    from tests.conftest import create_conversation

    await open_conversation(moderator_client, title="Consultation ouverte")
    await create_conversation(moderator_client, title="Encore en brouillon")

    page = await client.get("/")

    assert page.status_code == 200
    assert "Consultation ouverte" in page.text
    assert "Encore en brouillon" not in page.text


async def test_the_conversation_page_shows_no_opinion_groups(
    moderator_client, client
) -> None:
    """Décision de cadrage : pas d'affichage de groupes tant que l'appariement des
    clusters entre recalculs n'est pas fait (C6). Les étiquettes de k-means ne sont
    pas stables d'un run à l'autre."""
    _, slug, _ = await open_conversation(moderator_client, title="Les transports")

    page = await client.get(f"/c/{slug}")

    assert page.status_code == 200
    assert "Les transports" in page.text
    for interdit in ("groupe d'opinion", "Groupe 1", "cluster"):
        assert interdit not in page.text


async def test_an_unknown_conversation_page_is_404(client) -> None:
    assert (await client.get("/c/inexistante")).status_code == 404


async def test_a_draft_conversation_page_is_404(moderator_client, client) -> None:
    from tests.conftest import create_conversation

    await create_conversation(moderator_client, title="Brouillon")
    listing = await client.get("/")

    assert "Brouillon" not in listing.text


# --- comptes par formulaire ------------------------------------------------------


async def test_registering_from_the_form_logs_in_and_keeps_the_participant(
    moderator_client, client
) -> None:
    """L'inscription en fin de parcours ne doit pas faire perdre les votes déjà émis."""
    _, slug, statements = await open_conversation(moderator_client, statements=2)
    participant = (await client.get("/api/me")).json()["participant_id"]
    await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": statements[0], "value": 1},
    )

    response = await client.post(
        "/compte/inscription",
        data={"username": "zoe@exemple.fr", "password": PASSWORD, "suivant": f"/c/{slug}"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    # Depuis le J7, l'inscription passe par l'étape des thèmes — APRÈS la création du
    # compte, et passable d'un lien. Le retour porte la destination demandée : personne
    # ne perd le débat d'où il vient.
    assert response.headers["location"] == f"/mes-themes?retour=%2Fc%2F{slug}"
    identity = (await client.get("/api/me")).json()
    assert identity["authenticated"] is True          # connecté d'emblée
    assert identity["participant_id"] == participant  # et les votes suivent


async def test_registering_an_existing_address_is_refused_without_a_stack_trace(
    client,
) -> None:
    await register(client, "deja@exemple.fr")

    response = await client.post(
        "/compte/inscription",
        data={"username": "deja@exemple.fr", "password": PASSWORD},
    )

    assert response.status_code == 200
    assert "existe déjà" in response.text


async def test_login_from_the_form_merges_the_anonymous_votes(
    moderator_client, client_factory
) -> None:
    """La connexion par formulaire doit déclencher la MÊME fusion que l'API.

    Elle n'appelle pas le routeur fastapi-users, donc `on_after_login` ne joue pas :
    sans passage explicite par `establish_session`, la fusion serait sautée.
    """
    _, slug, statements = await open_conversation(moderator_client, statements=2)

    async with client_factory() as navigateur_a:
        await register(navigateur_a, "yann@exemple.fr")
        await login(navigateur_a, "yann@exemple.fr")
        compte = (await navigateur_a.get("/api/me")).json()["participant_id"]

    async with client_factory() as navigateur_b:
        await navigateur_b.get("/api/me")
        await navigateur_b.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statements[0], "value": -1},
        )
        await navigateur_b.post(
            "/compte/connexion",
            data={"username": "yann@exemple.fr", "password": PASSWORD},
            follow_redirects=False,
        )
        apres = (await navigateur_b.get("/api/me")).json()

    assert apres["participant_id"] == compte


async def test_a_wrong_password_shows_a_message_not_a_500(client) -> None:
    await register(client, "erreur@exemple.fr")

    response = await client.post(
        "/compte/connexion",
        data={"username": "erreur@exemple.fr", "password": "pas-le-bon"},
    )

    assert response.status_code == 200
    assert "incorrect" in response.text


# --- le lien de réinitialisation, de bout en bout --------------------------------


async def test_the_reset_email_points_at_a_real_page(client, outbox) -> None:
    await register(client, "lien@exemple.fr")
    outbox.clear()

    await client.post("/compte/mot-de-passe-oublie", data={"username": "lien@exemple.fr"})

    assert len(outbox) == 1
    assert "/compte/reinitialisation?token=" in link_in(outbox[0])


async def test_the_whole_reset_journey_works_from_the_emailed_link(
    client_factory, outbox
) -> None:
    """Le parcours réel : demande -> e-mail -> ouverture du lien -> formulaire ->
    connexion avec le nouveau mot de passe."""
    async with client_factory() as navigateur:
        await register(navigateur, "oubli@exemple.fr")
        outbox.clear()

        demande = await navigateur.post(
            "/compte/mot-de-passe-oublie",
            data={"username": "oubli@exemple.fr"},
            follow_redirects=True,  # POST/Redirect/GET depuis la revue des limites
        )
        # Ancré sur une phrase qui ne traverse pas un retour à la ligne du gabarit.
        assert "valable une heure" in demande.text

        lien = link_in(outbox[0])
        chemin = lien[lien.index("/compte/") :]

        # Le lien ouvre une VRAIE page, qui affiche un formulaire.
        page = await navigateur.get(chemin)
        assert page.status_code == 200
        assert 'name="token"' in page.text

        token = token_from(outbox[0])
        enregistre = await navigateur.post(
            "/compte/reinitialisation",
            data={
                "token": token,
                "password": "NouveauMotDePasse!2027",
                "confirmation": "NouveauMotDePasse!2027",
            },
        )
        assert "changé" in enregistre.text

    async with client_factory() as frais:
        assert (await login(frais, "oubli@exemple.fr", PASSWORD)).status_code == 400
        assert (
            await login(frais, "oubli@exemple.fr", "NouveauMotDePasse!2027")
        ).status_code == 204


async def test_merely_opening_the_link_does_not_consume_the_token(
    client_factory, outbox
) -> None:
    """Un antispam qui pré-charge le lien ne doit pas invalider la demande :
    le jeton n'est consommé qu'au POST."""
    async with client_factory() as navigateur:
        await register(navigateur, "antispam@exemple.fr")
        outbox.clear()
        await navigateur.post(
            "/compte/mot-de-passe-oublie", data={"username": "antispam@exemple.fr"}
        )
        token = token_from(outbox[0])

        await navigateur.get(f"/compte/reinitialisation?token={token}")  # « pré-chargé »

        enregistre = await navigateur.post(
            "/compte/reinitialisation",
            data={"token": token, "password": "EncoreUnAutre!2027",
                  "confirmation": "EncoreUnAutre!2027"},
        )
        assert "changé" in enregistre.text


async def test_mismatched_confirmation_is_refused_and_keeps_the_token(
    client, outbox
) -> None:
    await register(client, "confirm@exemple.fr")
    outbox.clear()
    await client.post("/compte/mot-de-passe-oublie", data={"username": "confirm@exemple.fr"})
    token = token_from(outbox[0])

    response = await client.post(
        "/compte/reinitialisation",
        data={"token": token, "password": "MotDePasse!2027", "confirmation": "Different!2027"},
    )

    assert "ne correspondent pas" in response.text
    assert token in response.text  # le formulaire est réaffiché, jeton compris


async def test_an_invalid_token_is_refused_gracefully(client) -> None:
    response = await client.post(
        "/compte/reinitialisation",
        data={"token": "jeton-bidon", "password": PASSWORD, "confirmation": PASSWORD},
    )

    assert response.status_code == 200
    assert "plus valable" in response.text


async def test_a_reset_link_without_a_token_is_handled(client) -> None:
    response = await client.get("/compte/reinitialisation")

    assert response.status_code == 200
    assert "incomplet" in response.text


# --- lien de vérification ---------------------------------------------------------


async def test_the_verification_link_confirms_the_address(client, outbox) -> None:
    await register(client, "confirme@exemple.fr")
    lien = link_in(outbox[0])
    assert "/compte/verification?token=" in lien

    page = await client.get(lien[lien.index("/compte/") :])

    assert page.status_code == 200
    assert "confirmée" in page.text
    assert (await client.get("/users/me")).status_code == 401  # non connecté, et pourtant


async def test_an_expired_verification_link_says_so(client) -> None:
    page = await client.get("/compte/verification?token=jeton-bidon")

    assert "plus valable" in page.text


# --- cookie anonyme sur les pages HTML -------------------------------------------


async def test_an_html_page_sets_the_anonymous_cookie(moderator_client, client) -> None:
    """Non-régression d'un défaut trouvé à C5.

    FastAPI ne recopie les en-têtes posés par une dépendance que lorsqu'il construit
    lui-même la réponse. Toutes les pages HTML renvoient directement une `Response` :
    le cookie du participant était donc perdu, et chaque visite créait un participant
    orphelin de plus. Un middleware l'écrit désormais sur la réponse émise.
    """
    from app.auth.users import ANON_COOKIE_NAME

    _, slug, _ = await open_conversation(moderator_client)

    page = await client.get(f"/c/{slug}")

    assert ANON_COOKIE_NAME in page.cookies


async def test_the_visitor_keeps_one_identity_across_pages_and_api(
    moderator_client, client, session_factory
) -> None:
    from sqlalchemy import func, select

    from app.models import Participant

    _, slug, _ = await open_conversation(moderator_client)

    await client.get("/")
    await client.get(f"/c/{slug}")
    premier = (await client.get("/api/me")).json()["participant_id"]
    await client.get(f"/c/{slug}")
    second = (await client.get("/api/me")).json()["participant_id"]

    assert premier == second
    async with session_factory() as session:
        total = await session.scalar(select(func.count(Participant.id)))
    # Un seul participant anonyme (l'autre est celui du modérateur).
    assert total == 2


# --- pages légales -----------------------------------------------------------------


async def test_the_legal_pages_are_reachable_and_name_the_host(client) -> None:
    """L'éditeur reste anonyme (LCEN 6 III 2°), mais l'hébergeur doit être nommé —
    c'est la contrepartie exacte de cet anonymat."""
    mentions = await client.get("/mentions-legales")

    assert mentions.status_code == 200
    assert "OVH SAS" in mentions.text
    assert "424 761 419" in mentions.text
    assert "2 rue Kellermann" in mentions.text
    # Aucune identité d'éditeur publiée.
    assert "réquisition" in mentions.text


async def test_the_privacy_policy_leaves_the_article_9_question_open(client) -> None:
    """Point non tranché, et qui doit le rester jusqu'à l'avis d'un juriste :
    la page ne doit ni affirmer ni nier que ces votes sont des opinions politiques."""
    page = await client.get("/confidentialite")

    assert page.status_code == 200
    assert "article 9" in page.text
    assert "pas tranchée" in page.text
    # Les durées de conservation, elles, sont annoncées.
    assert "24 heures" in page.text
    assert "14 jours" in page.text


async def test_la_politique_dit_ce_qui_est_garde_quand_on_signale(client) -> None:
    """La phrase du MOD-3a, écrite depuis le 12 septembre et jamais collée nulle part.

    Un traitement décrit dans le code et absent de la politique de confidentialité est un
    traitement non déclaré : la conservation d'une empreinte d'IP, même salée et bornée à
    trente jours, doit être annoncée à la personne qui signale — c'est l'article 13 du
    RGPD, et cela ne dépend d'aucun arbitrage en cours.
    """
    page = await client.get("/confidentialite")

    assert "Si vous signalez une proposition" in page.text
    # La phrase elle-même, telle qu'elle est écrite dans le service — pas une variante.
    from app.services.signalement import NOTE_CONFIDENTIALITE

    assert NOTE_CONFIDENTIALITE[:80] in page.text
    # Et la durée, dans le tableau de conservation, là où un lecteur la cherche.
    assert "30 jours au plus" in page.text


async def test_la_politique_a_sa_propre_date_de_mise_a_jour(client) -> None:
    """Elle a changé le 16 septembre, les mentions légales non. Une date partagée aurait
    daté d'aujourd'hui un document qui n'a pas bougé."""
    confidentialite = await client.get("/confidentialite")
    mentions = await client.get("/mentions-legales")

    assert "16 septembre 2026" in confidentialite.text
    assert "2 septembre 2026" in mentions.text
    assert "16 septembre 2026" not in mentions.text


async def test_every_page_links_to_the_legal_pages_and_to_erasure(client) -> None:
    """Un droit qu'on ne sait pas trouver n'est pas exercé : les trois liens sont dans
    le pied de page, donc sur toutes les pages."""
    accueil = await client.get("/")

    assert 'href="/mentions-legales"' in accueil.text
    assert 'href="/confidentialite"' in accueil.text
    assert 'href="/compte/suppression"' in accueil.text


# --- méthode HEAD ----------------------------------------------------------------


async def test_head_is_accepted_on_the_home_page_and_the_health_probe(client) -> None:
    """FastAPI n'ajoute pas HEAD aux routes GET — contrairement à Starlette.

    Sans `api_route(methods=["GET", "HEAD"])`, ces deux routes renvoient 405 et une
    sonde de supervision en HEAD conclut à une panne. Un retour à `@router.get`
    casserait ce test, ce qui est tout l'intérêt qu'il existe.
    """
    for chemin in ("/", "/health", "/health/db"):
        tete = await client.head(chemin)

        assert tete.status_code == 200, chemin
        assert tete.content == b"", f"HEAD {chemin} ne doit pas transporter de corps"


async def test_head_does_not_change_what_get_returns(client) -> None:
    """La correction ne devait rien changer à GET : même statut, même contenu."""
    for chemin in ("/", "/health", "/health/db"):
        obtenu = await client.get(chemin)

        assert obtenu.status_code == 200, chemin
        assert obtenu.content != b"", chemin


# --- accueil : tri et chiffres d'ambiance (chantier G2) ---------------------------


async def test_the_sort_falls_back_instead_of_failing(client):
    """Un tri inconnu affiche la page, il ne lève pas.

    Une URL partagée avec une faute de frappe (`?tri=recents`) doit rendre l'accueil,
    pas un 422 — c'est une adresse recopiée à la main, pas une attaque.
    """
    for parametre in ("", "?tri=recent", "?tri=votes", "?tri=nimportequoi", "?tri="):
        reponse = await client.get(f"/{parametre}")
        assert reponse.status_code == 200, (parametre, reponse.status_code)


async def test_sorting_by_votes_puts_the_most_voted_first(moderator_client, client):
    """« Le plus voté » classe par nombre de votes, et non par date.

    Le piège que ce test garde : une conversation SANS aucun vote n'a pas de ligne
    dans la sous-requête d'agrégat. Sans `coalesce`, elle sortirait avec un NULL, que
    PostgreSQL place AVANT tout le reste en ordre décroissant — la liste « la plus
    votée » commencerait donc par les débats que personne n'a votés.
    """
    from tests.conftest import open_conversation

    # La plus ancienne reçoit des votes ; la plus récente n'en reçoit aucun.
    _, ancienne, propositions = await open_conversation(
        moderator_client, statements=3, title="Ancienne mais votée"
    )
    _, recente, _ = await open_conversation(
        moderator_client, statements=3, title="Récente et muette"
    )

    for statement_id in propositions:
        reponse = await client.post(
            f"/api/conversations/{ancienne}/votes",
            json={"statement_id": statement_id, "value": 1},
        )
        assert reponse.status_code == 200, reponse.text

    par_date = (await client.get("/?tri=recent")).text
    par_votes = (await client.get("/?tri=votes")).text

    # Par date : la récente d'abord. Par votes : la votée d'abord.
    assert par_date.index("Récente et muette") < par_date.index("Ancienne mais votée")
    assert par_votes.index("Ancienne mais votée") < par_votes.index("Récente et muette")


async def test_open_statement_count_ignores_closed_conversations(
    moderator_client, client, session_factory
):
    """« N propositions ouvertes » ne compte que ce sur quoi on peut voter maintenant."""
    from sqlalchemy import select

    from app.models import Conversation, ConversationState
    from app.services import accueil as accueil_service
    from tests.conftest import open_conversation

    await open_conversation(moderator_client, statements=4, title="Toujours ouverte")
    _, slug_close, _ = await open_conversation(
        moderator_client, statements=6, title="Bientôt close"
    )

    async with session_factory() as session:
        assert await accueil_service.propositions_ouvertes(session) == 10
        conversation = await session.scalar(
            select(Conversation).where(Conversation.slug == slug_close)
        )
        conversation.state = ConversationState.closed
        await session.commit()
        # Les six propositions de la conversation close ne sont plus « ouvertes ».
        assert await accueil_service.propositions_ouvertes(session) == 4


async def test_weekly_participants_counts_people_not_votes(
    moderator_client, client, session_factory
):
    """Quelqu'un qui répond à trois propositions compte pour un, pas pour trois."""
    from app.services import accueil as accueil_service
    from tests.conftest import open_conversation

    _, slug, propositions = await open_conversation(moderator_client, statements=3)
    for statement_id in propositions:
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement_id, "value": 1},
        )

    async with session_factory() as session:
        assert await accueil_service.participants_de_la_semaine(session) == 1


def test_the_recompute_period_is_read_from_the_setting():
    """La phrase suit le réglage : écrite en dur, elle deviendrait fausse en silence."""
    from app.config import settings
    from app.services import accueil as accueil_service

    depart = settings.analysis_interval_seconds
    try:
        for secondes, attendu in (
            (3600, "toutes les heures"),
            (7200, "toutes les 2 heures"),
            (900, "toutes les 15 minutes"),
            (60, "toutes les minutes"),
            (90, "toutes les 90 secondes"),
        ):
            settings.analysis_interval_seconds = secondes
            assert accueil_service.periode_de_recalcul() == attendu
    finally:
        settings.analysis_interval_seconds = depart


# --- accueil : mini-barre de répartition (chantier G3) ----------------------------


async def test_the_bar_waits_rather_than_showing_an_empty_distribution(
    moderator_client, client, session_factory
):
    """Sans calcul abouti, la répartition est « en attente », pas vide.

    Une barre vide se lirait « personne » — ce qui est faux : il peut y avoir des
    votes sans qu'aucun groupe existe encore. Il faut quatre participants ayant
    chacun atteint le seuil de votes avant qu'un calcul aboutisse.
    """
    from app.services import accueil as accueil_service
    from tests.conftest import open_conversation

    _, slug, propositions = await open_conversation(moderator_client, statements=3)
    for statement_id in propositions:
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement_id, "value": 1},
        )

    async with session_factory() as session:
        from sqlalchemy import select

        from app.models import Conversation

        conversations = list(await session.scalars(select(Conversation)))
        part = (await accueil_service.repartitions(session, conversations))[
            conversations[0].id
        ]

    assert part.calculee is False
    assert part.segments == []
    assert part.n_votes == 3, "les votes se comptent même sans calcul"

    page = (await client.get("/")).text
    assert "segment--attente" in page
    assert "en attente" in page


async def test_the_bar_is_ordered_by_size_like_its_colours(
    moderator_client, client, session_factory
):
    """Les segments descendent par effectif, et leurs parts font 100 %.

    Depuis le G10, la teinte suit le rang d'effectif (décision du client). L'ordre des
    segments suit le même classement : avec des teintes rangées par taille et des
    segments rangés par identité, la barre montrerait les couleurs de la palette dans
    le désordre, et son plus large segment pourrait se trouver au milieu.
    """
    from sqlalchemy import select

    from app.analysis import pipeline
    from app.models import Conversation
    from app.services import accueil as accueil_service
    from tests.conftest import open_conversation
    from tests.test_carte import _peupler

    conversation_id, _, propositions = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, propositions, prefixe="barre")
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2
        await session.commit()
        await pipeline.analyse(session, conversation)

    async with session_factory() as session:
        conversations = list(await session.scalars(select(Conversation)))
        part = (await accueil_service.repartitions(session, conversations))[
            conversation_id
        ]

    assert part.calculee is True
    assert part.n_groupes >= 2
    # Rangés du plus grand au plus petit, le nom départageant les égalités.
    effectifs = [(-s.effectif, s.nom) for s in part.segments]
    assert effectifs == sorted(effectifs), "la barre n'est pas rangée par effectif"
    # La teinte est celle que le groupe porte sur la CARTE de sa page, et le premier
    # segment — le plus grand groupe — porte la première couleur de la palette.
    from app.services import carte_rendu

    assert part.segments[0].couleur == carte_rendu.PALETTE[0]
    couleurs = [s.couleur for s in part.segments]
    assert len(set(couleurs)) == len(couleurs), "deux groupes de la même teinte"
    for couleur in couleurs:
        assert couleur in carte_rendu.PALETTE or couleur == carte_rendu.GRIS_RELIQUAT
    assert abs(sum(s.part for s in part.segments) - 100) < 0.5


async def test_the_home_bar_and_the_map_agree_on_every_colour(
    moderator_client, client, session_factory
) -> None:
    """La promesse du 5 septembre 2026 : le groupe A a la même teinte sur les deux pages.

    C'était l'écart assumé du G3 — la mini-barre employait encre/or/jaune/gris, la
    carte la palette des groupes, si bien que le même débat se peignait de deux façons
    à une page d'intervalle. Le client l'a levé. Ce test compare les deux sources sur
    les mêmes données plutôt que de vérifier que chacune, séparément, « emploie la
    palette » : c'est l'accord entre elles qui a été demandé.
    """
    from app.analysis import pipeline
    from app.models import Conversation
    from app.services import accueil as accueil_service
    from app.services import carte as carte_service
    from app.services import carte_rendu
    from tests.conftest import open_conversation
    from tests.test_carte import _peupler

    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8, title="Accord des teintes"
    )
    async with session_factory() as session:
        await _peupler(session, statements, prefixe="teintes")
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 3
        await session.commit()
        await pipeline.analyse(session, conversation)

        barre = (await accueil_service.repartitions(session, [conversation]))[
            conversation_id
        ]
        carte = await carte_service.carte(session, conversation)
        sur_la_carte = carte_rendu.couleurs_par_groupe(carte.groupes)

    assert barre.calculee and barre.segments
    for segment in barre.segments:
        assert segment.couleur == sur_la_carte[segment.nom], (
            f"groupe {segment.nom} : {segment.couleur} sur l'accueil, "
            f"{sur_la_carte[segment.nom]} sur la carte"
        )


def test_a_debate_is_new_for_a_week():
    """L'étiquette « Nouveau » tient sept jours — la fenêtre de « cette semaine »."""
    from datetime import datetime, timedelta, timezone

    from app.models import Conversation
    from app.services import accueil as accueil_service

    maintenant = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    for age, attendu in ((0, True), (6, True), (8, False), (30, False)):
        conversation = Conversation(
            slug="x", title="x", created_at=maintenant - timedelta(days=age)
        )
        assert accueil_service.est_nouvelle(conversation, maintenant) is attendu, age


def test_the_badge_says_how_long_the_debate_has_been_open():
    """L'étiquette dit DEPUIS QUAND, et plus seulement « Nouveau » (chantier I2).

    Le mot ne disait pas à quel point : deux débats ouverts à six jours d'écart le
    portaient à l'identique. La fenêtre, elle, ne bouge pas — c'est celle
    d'`est_nouvelle`, sept jours, et au-delà il n'y a plus d'étiquette du tout.
    """
    from datetime import datetime, timedelta, timezone

    from app.models import Conversation
    from app.services import accueil as accueil_service

    maintenant = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    attendus = {
        0: "Ouvert aujourd'hui",
        1: "Ouvert hier",
        2: "Ouvert il y a 2 jours",
        6: "Ouvert il y a 6 jours",
        # Au-delà de la fenêtre : plus rien, comme « Nouveau » disparaissait.
        7: None,
        30: None,
    }
    for age, attendu in attendus.items():
        conversation = Conversation(
            slug="x", title="x", created_at=maintenant - timedelta(days=age)
        )
        assert accueil_service.depuis_ouverture(conversation, maintenant) == attendu, age

    # Quelques heures, ce n'est pas encore un jour : « il y a 0 jour » ne se dit pas.
    presque = Conversation(
        slug="x", title="x", created_at=maintenant - timedelta(hours=5)
    )
    assert accueil_service.depuis_ouverture(presque, maintenant) == "Ouvert aujourd'hui"


# --- la carte de vote (chantier I2, instruction 10) -------------------------------


async def test_a_vote_answers_with_the_live_tally_of_that_statement(
    client, moderator_client
) -> None:
    """Le résultat immédiat compte les votes RÉELS de la proposition, celui-ci compris.

    Les comptes viennent de la table `vote`, pas de `statement_stat` : celle-ci est
    écrite par le calcul, au mieux toutes les heures, et ne contiendrait donc pas le
    vote qu'on vient d'émettre — un résultat qui oublie le vote de celui à qui on le
    montre serait faux de la pire façon.
    """
    from tests.conftest import open_conversation

    _, slug, statements = await open_conversation(
        moderator_client, statements=4, title="Débat mesuré"
    )
    reponse = await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": statements[0], "value": 1},
    )
    resultat = reponse.json()["resultat"]
    assert resultat == {
        "n_accord": 1,
        "n_desaccord": 0,
        "n_passe": 0,
        "total": 1,
        "part_accord": 100,
        "part_desaccord": 0,
        "part_passe": 0,
    }, "un seul vote, et c'est le sien"

    # Les trois parts somment toujours à 100 — c'est `resultats.pourcentages` qui
    # arrondit, la même fonction que la page de résultats.
    autre = await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": statements[1], "value": 0},
    )
    parts = autre.json()["resultat"]
    assert parts["part_accord"] + parts["part_desaccord"] + parts["part_passe"] == 100


async def test_the_progress_threshold_is_the_one_of_THIS_debate(
    client, moderator_client
) -> None:
    """Le seuil annoncé n'est jamais le 7 fixe de la maquette.

    `min_user_votes_for` le borne par le nombre de propositions : sur un débat de trois,
    demander sept votes serait une consigne intenable — personne ne pourrait la suivre.
    """
    from tests.conftest import open_conversation

    _, petit, propositions = await open_conversation(
        moderator_client, statements=3, title="Petit débat"
    )
    page = (await client.get(f"/c/{petit}")).text
    assert 'data-seuil="2"' in page, "trois propositions : le seuil descend à 2"
    assert 'data-seuil="7"' not in page

    reponse = await client.post(
        f"/api/conversations/{petit}/votes",
        json={"statement_id": propositions[0], "value": 1},
    )
    corps = reponse.json()
    assert corps["seuil_situe"] == 2
    assert corps["votes_emis"] == 1


async def test_the_meta_line_only_attributes_what_the_model_really_knows(
    client, moderator_client
) -> None:
    """Les deux drapeaux d'origine voyagent SÉPARÉMENT jusqu'à l'interface.

    Une proposition d'un participant porte un `author_participant_id` ; tout ce que la
    modération ajoute est marqué `is_seed`, y compris en cours de route — vérifié ici.

    Le modèle permet pourtant un troisième état, ni l'un ni l'autre, qu'aucun écran ne
    produit aujourd'hui. C'est pourquoi l'interface n'écrit son attribution que sur un
    drapeau POSÉ, et se tait sur ce troisième cas, au lieu de déduire « écrite par un
    participant » de « ce n'est pas une amorce » : le jour où une route produira cet
    état, elle se taira au lieu d'attribuer à un participant un texte qui n'est pas de
    lui — un mensonge sur l'origine d'un texte, ce qu'une plateforme de débat ne peut
    pas se permettre.
    """
    from tests.conftest import open_conversation

    # `post` : la proposition d'un participant est publiée tout de suite. En
    # pré-modération elle resterait en attente, donc jamais servie au vote — et ce test
    # a justement besoin de la voir telle qu'elle est servie.
    conversation_id, slug, _ = await open_conversation(
        moderator_client, statements=1, title="Débat attribué", mode="post"
    )
    # Une proposition d'un participant…
    ajout = await client.post(
        f"/api/conversations/{slug}/statements",
        json={"text": "Une proposition écrite par quelqu'un qui passe par là."},
    )
    assert ajout.status_code in (200, 201)
    # …et une de la modération, ajoutée APRÈS l'ouverture.
    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}/statements",
        data={"text": "Une proposition ajoutée par la modération."},
        follow_redirects=False,
    )

    # On parcourt tout le débat pour voir chaque proposition telle qu'elle est servie.
    vues = {}
    while True:
        vue = (await client.get(f"/api/conversations/{slug}/next-statement")).json()
        proposition = vue["statement"]
        if proposition is None:
            break
        vues[proposition["text"]] = (
            proposition["is_seed"],
            proposition["par_un_participant"],
        )
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": proposition["id"], "value": 0},
        )

    assert vues["Une proposition écrite par quelqu'un qui passe par là."] == (False, True)
    assert vues["Une proposition ajoutée par la modération."] == (True, False), (
        "ce que la modération ajoute est une amorce, même après l'ouverture"
    )
    amorce = next(v for texte, v in vues.items() if texte.startswith("Proposition d'amorce"))
    assert amorce == (True, False)
    # Aucune proposition n'est jamais les deux à la fois : l'attribution est toujours
    # décidable, et jamais ambiguë.
    assert all(not (seed and participant) for seed, participant in vues.values())


async def test_the_recap_counts_this_persons_votes_on_this_debate(
    client, moderator_client
) -> None:
    """« Votre parcours » compte les votes de CETTE personne sur CE débat.

    Les trois valeurs sont celles que `Vote.value` peut prendre — aucune sous-catégorie
    n'est déduite. Le total est leur somme, et c'est le même nombre que celui de la
    barre de progression : les deux blocs lisent la même requête, sans quoi ils
    finiraient par afficher deux chiffres différents sur le même écran.
    """
    from tests.conftest import open_conversation

    _, slug, statements = await open_conversation(
        moderator_client, statements=4, title="Débat parcouru"
    )

    page = (await client.get(f"/c/{slug}")).text
    assert "Aucun vote pour l'instant" in page
    assert 'data-total="0"' in page

    for statement_id, valeur in zip(statements, (1, -1, 0)):
        corps = (
            await client.post(
                f"/api/conversations/{slug}/votes",
                json={"statement_id": statement_id, "value": valeur},
            )
        ).json()
    assert corps["parcours"] == {
        "accord": 1,
        "desaccord": 1,
        "passe": 1,
        "total": 3,
    }
    # Le total du parcours EST le compte de la barre de progression.
    assert corps["votes_emis"] == corps["parcours"]["total"]

    # Et le serveur écrit la même chose au rechargement : la page dit vrai sans script.
    page = (await client.get(f"/c/{slug}")).text
    assert 'data-total="3"' in page
    assert "3 votes sur ce débat" in page.replace("&nbsp;", " ")


async def test_the_thank_you_takes_the_place_of_the_vote_card(
    client, moderator_client
) -> None:
    """Le remerciement s'écrit LÀ où était la dernière proposition.

    Les deux blocs se suivent dans le gabarit, avant la grille à deux colonnes : l'un se
    cache au moment où l'autre paraît, et le regard n'a pas à descendre d'un écran pour
    retrouver la suite. Jusqu'au chantier I2 le remerciement vivait après la grille.
    """
    from tests.conftest import open_conversation

    _, slug, _ = await open_conversation(moderator_client, statements=2)
    page = (await client.get(f"/c/{slug}")).text

    vote = page.index('id="zone-vote"')
    fin = page.index('id="zone-fin"')
    grille = page.index('class="grille-debat"')
    assert vote < fin < grille, (
        "le remerciement suit immédiatement la carte de vote, avant la grille"
    )


async def test_the_vote_buttons_never_move_with_the_text_length(
    client, moderator_client
) -> None:
    """Les deux réponses sont toujours au même endroit de la carte (chantier I2, 16).

    Tout ce qui varie d'une proposition à l'autre — le chargement, la ligne de méta, le
    texte, ses sources — est enfermé dans une boîte de hauteur FIXE, posée avant les
    réponses. Une phrase de dix mots et une de cinquante donnent donc le même écran :
    « D'accord » et « Pas d'accord » ne se dérobent pas sous la main entre deux votes.

    Le test garde les deux moitiés de la règle : la structure du gabarit, et la hauteur
    fixe dans la feuille de style — une `min-height` suffirait à laisser les boutons
    redescendre dès qu'un texte dépasse la réserve.
    """
    from html.parser import HTMLParser
    from pathlib import Path

    from tests.conftest import open_conversation

    _, slug, _ = await open_conversation(moderator_client, statements=2)
    page = (await client.get(f"/c/{slug}")).text

    class Arbre(HTMLParser):
        """Retient, pour chaque élément intéressant, la pile de ses ancêtres."""

        def __init__(self) -> None:
            super().__init__()
            self.pile: list[str] = []
            self.ancetres: dict[str, list[str]] = {}
            self.ordre: list[str] = []

        def handle_starttag(self, tag, attrs) -> None:
            attributs = dict(attrs)
            nom = attributs.get("id") or attributs.get("class", "")
            # Les éléments vides ne se ferment jamais : les empiler décalerait tout
            # l'arbre, et un élément se croirait dans la boîte sans y être.
            if tag not in VIDES:
                self.pile.append(nom)
            if nom in SUIVIS:
                self.ancetres[nom] = list(self.pile[:-1])
                self.ordre.append(nom)

        def handle_endtag(self, tag) -> None:
            if self.pile:
                self.pile.pop()

    SUIVIS = {"zone-proposition", "meta-vote", "texte", "sources", "choix-de-vote"}
    VIDES = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
             "meta", "param", "source", "track", "wbr"}
    arbre = Arbre()
    arbre.feed(page)

    for variable in ("meta-vote", "texte", "sources"):
        assert "zone-proposition" in arbre.ancetres[variable], (
            f"{variable} varie d'une proposition à l'autre : hors de la boîte à hauteur "
            "fixe, il déplacerait les deux réponses"
        )
    assert "zone-proposition" not in arbre.ancetres["choix-de-vote"], (
        "les réponses sont HORS de la boîte : dedans, elles défileraient avec le texte"
    )
    assert arbre.ordre.index("zone-proposition") < arbre.ordre.index("choix-de-vote")

    feuille = Path("app/static/styles.css").read_text()
    regle = feuille[feuille.index(".zone-proposition {") :]
    regle = regle[: regle.index("}")]
    assert "height: calc(var(--lignes-proposition)" in regle, (
        "la boîte doit avoir une hauteur FIXE calculée en lignes de proposition ; une "
        "`min-height` laisserait les boutons redescendre sous un texte plus long"
    )
    assert "min-height" not in regle
    assert "overflow-y: auto" in regle, (
        "un texte plus long que la réserve doit défiler DANS la boîte, jamais être tronqué"
    )


async def test_no_related_debate_block_is_ever_rendered(
    client, moderator_client
) -> None:
    """« Débat lié » de la maquette n'existe pas, et son absence est gardée.

    Aucune relation entre débats n'existe côté base ; le chantier I l'avait déjà écarté
    pour cette raison. Le fabriquer demanderait de modéliser cette relation — une
    fonctionnalité, pas une retouche d'habillage. Ce test empêche qu'un lien arbitraire
    se glisse là un jour au nom de la ressemblance avec la maquette.
    """
    from tests.conftest import open_conversation

    _, slug, _ = await open_conversation(moderator_client, statements=2)
    page = (await client.get(f"/c/{slug}")).text
    assert "Débat lié" not in page
    assert 'class="related"' not in page


# --- navigation et pages d'explication (chantier G4) ------------------------------


async def test_la_page_comment_ca_marche_porte_son_texte_et_ses_trois_schemas(client):
    """La page n'annonce plus qu'elle reste à écrire (chantier CCM) : elle porte

    désormais les cinq sections et les trois schémas SVG fournis par le client.
    """
    reponse = await client.get("/comment-ca-marche")

    assert reponse.status_code == 200
    assert "Comment ça marche" in reponse.text
    assert "À DÉFINIR" not in reponse.text
    for titre_section in (
        "Un débat, des propositions courtes",
        "Voter : d'accord, pas d'accord, ou on passe",
        "Les groupes d'opinion se révèlent",
        "La modération",
        "Les comptes et la progression",
    ):
        assert titre_section in reponse.text
    for id_schema in ('id="svg-1"', 'id="svg-3"', 'id="bloc-4"'):
        assert id_schema in reponse.text


async def test_every_page_offers_the_same_navigation(client):
    """La navigation est dans l'en-tête, donc sur toutes les pages et à toute taille.

    Ce test garde la régression que le retrait de la barre d'onglets aurait pu créer :
    avant lui, l'en-tête d'un téléphone ne portait qu'un seul lien — la marque — et
    tout le reste vivait dans la barre du bas. La retirer sans remonter la navigation
    aurait laissé un visiteur au téléphone sans accès à son compte.
    """
    for chemin in ("/", "/comment-ca-marche", "/mentions-legales", "/compte/connexion"):
        page = (await client.get(chemin)).text
        assert 'class="nav-site"' in page, chemin
        for destination in ('href="/"', 'href="/debats"', 'href="/comment-ca-marche"'):
            assert destination in page, (chemin, destination)
        # Et l'accès au compte, qui est ce que la barre d'onglets portait.
        assert "/compte/connexion" in page, chemin
    # La barre d'onglets a bien disparu.
    assert 'class="onglets"' not in (await client.get("/")).text


async def test_the_debates_entry_leads_to_a_real_page(moderator_client, client):
    """« Débats » mène à une page qui existe, et qui porte la liste.

    Le G4 l'avait fait pointer sur l'ancre `/#debats` de l'accueil, faute de seconde
    page. Depuis que l'accueil ne montre que cinq débats (G10), cette page est ce qui
    rend les autres atteignables : un lien de navigation vers une 404 les perdrait
    tous.
    """
    await open_conversation(moderator_client, title="Un débat listé")

    reponse = await client.get("/debats")

    assert reponse.status_code == 200
    assert "Un débat listé" in reponse.text
    # Et les deux mêmes onglets que l'accueil, avec leurs adresses à elles.
    assert 'href="/debats?tri=recent"' in reponse.text
    assert 'href="/debats?tri=votes"' in reponse.text


# --- fin de parcours : bandeau de statut ------------------------------------------


async def test_the_end_of_the_journey_has_no_other_debates_panel(
    moderator_client, client
) -> None:
    """Le panneau « En attendant, d'autres débats » a été retiré de la fin de
    parcours : quelqu'un qui vient de voter n'a plus qu'un statut à lire, pas une
    invitation à aller voir ailleurs."""
    _, slug, _ = await open_conversation(moderator_client, title="Les transports")

    page = (await client.get(f"/c/{slug}")).text

    assert "En attendant, d'autres débats" not in page
    assert 'class="bloc-autres-debats"' not in page


async def test_the_end_of_the_journey_confirms_votes_are_saved(
    moderator_client, client
) -> None:
    """Le bandeau de statut confirme que les votes sont enregistrés, juste après
    « Merci », avant l'annonce du groupe."""
    _, slug, _ = await open_conversation(moderator_client, title="Les transports")

    page = (await client.get(f"/c/{slug}")).text

    assert "Merci ! Vos votes sont enregistrés !" in page


# --- l'accueil en vitrine, et la page « Débats » (chantier G10) --------------------


async def _plusieurs_debats(moderator_client, combien: int, prefixe="Débat") -> None:
    """Des débats numérotés, créés du plus ancien au plus récent."""
    for index in range(combien):
        await open_conversation(moderator_client, title=f"{prefixe} {index:02d}")


async def test_the_home_page_shows_only_five_debates(moderator_client, client) -> None:
    """L'accueil est une vitrine, pas un catalogue (décision du client, G10).

    Cinq débats, et un lien vers la liste complète — qui emporte le tri courant :
    quelqu'un qui regarde « Le plus voté » veut la suite du plus voté, pas le début
    d'un autre classement.
    """
    from app.services import accueil as accueil_service

    await _plusieurs_debats(moderator_client, 8, prefixe="Vitrine")

    page = (await client.get("/?tri=votes")).text

    montres = [f"Vitrine {i:02d}" for i in range(8) if f"Vitrine {i:02d}" in page]
    assert len(montres) == accueil_service.LIMITE_ACCUEIL == 5
    assert 'href="/debats?tri=votes"' in page, "le lien ne garde pas le tri courant"


async def test_the_home_page_hides_the_link_when_it_shows_everything(
    moderator_client, client
) -> None:
    """Trois débats sur cinq places : « Voir tous les débats » ne promet rien de plus."""
    await _plusieurs_debats(moderator_client, 3, prefixe="Court")

    page = (await client.get("/")).text

    assert "Court 02" in page
    assert 'class="actions voir-tout"' not in page


async def test_the_debates_page_shows_ten_then_loads_the_rest(
    moderator_client, client
) -> None:
    """Dix à l'arrivée, la suite en descendant — et jamais deux fois le même débat.

    C'est la propriété que la pagination par décalage peut perdre : si deux débats que
    l'ordre ne départage pas s'échangent d'une requête à l'autre, une fournée en saute
    un et l'autre le montre deux fois. Tous les débats de ce test ont zéro vote et sont
    créés dans la même seconde — c'est exactement le cas ambigu, et l'identifiant qui
    clôt l'ordre est ce qui le tranche.
    """
    from app.services import accueil as accueil_service

    await _plusieurs_debats(moderator_client, 23, prefixe="File")

    page = (await client.get("/debats")).text
    premiers = [f"File {i:02d}" for i in range(23) if f"File {i:02d}" in page]
    assert len(premiers) == accueil_service.LIMITE_DEBATS == 10

    vus = list(premiers)
    decalage = len(premiers)
    for _ in range(3):
        suite = (await client.get(f"/debats/suite?tri=recent&decalage={decalage}")).json()
        fournee = [f"File {i:02d}" for i in range(23) if f"File {i:02d}" in suite["html"]]
        assert not (set(fournee) & set(vus)), f"débat servi deux fois : {fournee}"
        vus += fournee
        decalage += suite["nombre"]
        if not suite["encore"]:
            break

    assert len(vus) == 23, "la file n'a pas rendu tous les débats"
    assert len(set(vus)) == 23
    assert suite["encore"] is False


async def test_the_more_link_works_without_javascript(
    moderator_client, client
) -> None:
    """Le lien de repli montre la MÊME chose que le défilement, pas une autre page.

    L'adresse dit « montre-m'en tant », et non « montre-moi la page trois » : sans
    script, on continue donc la liste au lieu de perdre les premiers débats. Un numéro
    de page aurait donné deux écrans différents pour un même geste.
    """
    await _plusieurs_debats(moderator_client, 15, prefixe="Repli")

    page = (await client.get("/debats?nombre=20")).text

    montres = [f"Repli {i:02d}" for i in range(15) if f"Repli {i:02d}" in page]
    assert len(montres) == 15, "le repli n'a pas déroulé la liste depuis le début"
    assert "Vous avez vu tous les débats." in page


async def test_the_displayed_count_is_capped(moderator_client, client) -> None:
    """Le nombre vient de l'URL, donc il se borne.

    Sans plafond, `?nombre=99999` ferait rendre le catalogue entier — mini-barre et
    répartition comprises — sur une requête anonyme.
    """
    from app.services import accueil as accueil_service

    assert accueil_service.nombre_valide(99999) == accueil_service.PLAFOND_DEBATS
    assert accueil_service.nombre_valide(-4) == 1
    assert accueil_service.nombre_valide(-4, mini=0) == 0
    # Et une valeur aberrante affiche la page au lieu de lever, comme pour le tri.
    assert (await client.get("/debats?nombre=99999&tri=nimporte")).status_code == 200


async def test_both_pages_render_the_same_card(moderator_client, client) -> None:
    """La carte d'un débat est écrite une fois, et rendue par les trois chemins.

    Recopiée dans chaque gabarit, elle aurait divergé — et le même débat n'aurait pas
    eu la même allure selon la page où on le croise, ni selon qu'on l'a vu au
    chargement ou en descendant la file.
    """
    await _plusieurs_debats(moderator_client, 12, prefixe="Meme")

    accueil = (await client.get("/")).text
    liste = (await client.get("/debats")).text
    fournee = (await client.get("/debats/suite?tri=recent&decalage=10")).json()["html"]

    for page in (accueil, liste, fournee):
        assert 'class="card carte-debat"' in page
        assert 'class="barre-groupes"' in page
        assert "meta-debat" in page


# --- les seuils du régime dégradé, publiés (MOD-18) ---------------------------------
#
# Le plan v2 du chantier Modération (§9) l'exige en toutes lettres : « Écris ces seuils
# publiquement dès la première version. C'est ce qui transforme "je garde ces privilèges
# pour l'instant" en une promesse vérifiable par n'importe qui. » Une promesse publique
# qu'aucun test ne garde se perd au premier remaniement de gabarit.


async def test_les_regles_de_moderation_ne_sont_plus_a_definir(client) -> None:
    page = await client.get("/regles-de-moderation")

    assert page.status_code == 200
    assert "À DÉFINIR" not in page.text
    assert "Il ne s'allume qu'à partir de" in page.text


async def test_la_page_annonce_les_seuils_qui_retirent_ses_privileges_au_responsable(
    client,
) -> None:
    """Ce sont ceux-là qui comptent : sans eux, « le responsable tranche pour l'instant »
    est une phrase que rien ne vient jamais contredire."""
    page = await client.get("/regles-de-moderation")

    for seuil in ("20 comptes vérifiés actifs", "15 relecteurs", "8 arbitres", "300 participants"):
        assert seuil in page.text


async def test_la_page_decrit_ce_que_le_site_fait_et_non_ce_que_le_plan_prevoyait(
    client,
) -> None:
    """**La règle du MOD-3b, appliquée à une page publique** : aucun texte n'annonce un
    effet que le code ne produit pas.

    Deux lignes du plan disaient autre chose que la production. Le plan écrivait « tu
    demandes la reformulation à l'auteur, tu ne l'écris jamais à sa place » ; depuis le
    MOD-10, le responsable l'écrit et l'auteur décide. C'est donc cela qui est publié."""
    page = await client.get("/regles-de-moderation")

    assert "Le responsable propose une reformulation" in page.text
    assert "l'auteur seul décide" in page.text.replace("&#39;", "'")
    # Et la publication immédiate, acquise au MOD-14.
    assert "Il n'y a pas de relecture avant publication" in page.text.replace("&#39;", "'")


async def test_la_page_ne_publie_aucun_chiffre_de_population(client) -> None:
    """Les seuils disent à partir de QUAND un mécanisme s'allume. Dire combien de comptes
    vérifiés existent aujourd'hui renseignerait surtout qui voudrait profiter de leur
    petit nombre — le plan demande les seuils, pas l'état des effectifs."""
    page = await client.get("/regles-de-moderation")

    for indiscretion in ("compte vérifié aujourd'hui", "1 compte vérifié", "participants inscrits"):
        assert indiscretion not in page.text


async def test_la_page_porte_les_trois_invariants(client) -> None:
    """Ce qui ne changera pas avec la taille du site. Les publier est ce qui les rend
    opposables ; les garder par un test est ce qui les empêche de disparaître d'un
    remaniement."""
    page = await client.get("/regles-de-moderation")
    texte = page.text.replace("&#39;", "'")

    assert "Le vote n'est jamais restreint" in texte
    assert "Signaler reste ouvert sans compte" in texte
    assert "jamais réécrite sans l'accord de son auteur" in texte
