"""La liste fermée des thèmes, et les règles qui la gardent cohérente.

Le J1 ne pose aucun écran : ce qui est éprouvé ici est la constante elle-même, la
validation d'un étiquetage, et le fait que les tables tiennent leurs promesses.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Conversation, ConversationTheme, Participant
from app.services.themes import (
    PAR_CODE,
    THEMES,
    THEMES_MAX,
    THEMES_MIN,
    libelle,
    themes_connus,
    valider_choix,
)


def test_the_list_has_twenty_one_entries_with_unique_codes() -> None:
    """Le compte est une décision du client, pas un hasard : il est figé ici.

    Et l'unicité des codes n'est garantie par rien d'autre — `PAR_CODE` étant un
    dictionnaire, un doublon y passerait en silence en écrasant le premier.
    """
    assert len(THEMES) == 21
    assert len(PAR_CODE) == 21


def test_codes_are_url_safe_and_short() -> None:
    """Le code va dans l'URL et dans une colonne de 32 caractères.

    Un code accentué ou espacé donnerait une adresse encodée en pourcents, illisible
    dans un lien partagé — et le filtre de l'accueil est fait pour être partagé.
    """
    for theme in THEMES:
        assert theme.code.isascii()
        assert theme.code.islower()
        assert theme.code.isalpha()
        assert len(theme.code) <= 32


def test_every_theme_carries_a_boundary_for_the_moderator() -> None:
    """L'aide n'est pas de la documentation morte : c'est ce qui rend l'étiquetage
    reproductible d'un modérateur à l'autre. Un thème sans frontière écrite est un
    thème dont deux personnes ne feront pas le même usage."""
    for theme in THEMES:
        assert theme.libelle
        assert len(theme.aide) > 40, theme.code


def test_a_retired_code_still_displays_instead_of_raising() -> None:
    """Un code retiré de la constante survit en base. La page d'un débat étiqueté
    l'an dernier doit s'afficher, pas rendre une erreur."""
    assert libelle("immigration") == "immigration"
    assert themes_connus(["sante", "immigration"]) == ["sante"]


def test_display_order_follows_the_list_not_the_caller() -> None:
    """Deux débats portant les mêmes thèmes doivent montrer leurs étiquettes dans le
    même ordre, sinon les comparer à l'œil sur une liste devient impossible."""
    assert themes_connus(["sport", "sante"]) == themes_connus(["sante", "sport"])
    assert themes_connus(["sport", "sante"]) == ["sante", "sport"]


def test_duplicates_are_collapsed() -> None:
    assert themes_connus(["sante", "sante"]) == ["sante"]


def test_validating_a_submission_refuses_what_display_tolerates() -> None:
    """`themes_connus` sert à AFFICHER l'existant, `valider_choix` à ÉCRIRE.

    Le premier ignore un code inconnu, le second le refuse : accepter en silence une
    case inconnue reviendrait à enregistrer un étiquetage que l'auteur n'a pas voulu.
    """
    assert themes_connus(["sante", "inconnu"]) == ["sante"]
    with pytest.raises(ValueError):
        valider_choix(["sante", "inconnu"])


def test_a_debate_needs_between_one_and_three_themes() -> None:
    """Zéro thème rend le débat invisible au filtre — c'est ce qui fonde
    l'obligation ; au-delà de trois, l'étiquetage ne trie plus rien."""
    assert THEMES_MIN == 1 and THEMES_MAX == 3
    with pytest.raises(ValueError):
        valider_choix([])
    with pytest.raises(ValueError):
        valider_choix(None)
    with pytest.raises(ValueError):
        valider_choix(["sante", "logement", "transports", "energie"])
    assert valider_choix(["sante"]) == ["sante"]
    assert valider_choix(["transports", "sante", "logement"]) == [
        "sante", "logement", "transports",
    ]


def test_three_duplicates_of_one_code_are_one_theme_not_three() -> None:
    """Le plafond se compte en thèmes DISTINCTS. Un formulaire qui renvoie trois fois
    la même case ne doit pas être refusé pour dépassement."""
    assert valider_choix(["sante", "sante", "sante"]) == ["sante"]


# --- ce que la base tient elle-même ---------------------------------------------


async def test_a_debate_cannot_carry_the_same_theme_twice(session_factory) -> None:
    """La contrainte est portée par la base et non par le seul contrôle de saisie :
    le back-office et la console ne passent pas par le formulaire."""
    async with session_factory() as session:
        conversation = Conversation(slug="doublon", title="Doublon")
        session.add(conversation)
        await session.flush()
        session.add_all([
            ConversationTheme(conversation_id=conversation.id, code="sante"),
            ConversationTheme(conversation_id=conversation.id, code="sante"),
        ])
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_deleting_a_debate_takes_its_themes_with_it(session_factory) -> None:
    async with session_factory() as session:
        conversation = Conversation(slug="ephemere", title="Éphémère")
        conversation.themes = [ConversationTheme(code="sante")]
        session.add(conversation)
        await session.commit()
        conversation_id = conversation.id

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        await session.delete(conversation)
        await session.commit()

    from sqlalchemy import func, select

    async with session_factory() as session:
        restants = await session.scalar(
            select(func.count(ConversationTheme.id)).where(
                ConversationTheme.conversation_id == conversation_id
            )
        )
        assert restants == 0


async def test_the_debate_orders_its_labels_by_the_list(session_factory) -> None:
    """`codes_themes` doit rendre l'ordre de la constante, quel que soit l'ordre
    d'insertion — c'est la propriété sur laquelle l'affichage s'appuie."""
    async with session_factory() as session:
        conversation = Conversation(slug="ordre", title="Ordre")
        conversation.themes = [
            ConversationTheme(code="sport"),
            ConversationTheme(code="sante"),
        ]
        session.add(conversation)
        await session.commit()
        assert conversation.codes_themes == ["sante", "sport"]


async def test_preferences_live_on_the_participant_and_survive_the_account(
    session_factory, client_factory
) -> None:
    """La décision structurante du chantier, éprouvée de bout en bout.

    Un anonyme règle ses thèmes ; il crée ensuite un compte. C'est la MÊME ligne
    `participant` qui est rattachée au compte, donc la préférence le suit sans qu'un
    transfert ait été écrit nulle part. Rangée sur `user`, elle aurait été perdue pour
    tous ceux qui règlent leurs thèmes avant de s'inscrire — c'est-à-dire l'ordre
    naturel des choses.
    """
    from sqlalchemy import select

    from tests.conftest import register

    async with client_factory() as client:
        # `/api/me` répond toujours 200 et fait naître le participant anonyme du
        # cookie. Une page publique ne suffirait pas : l'accueil n'en crée pas.
        assert (await client.get("/api/me")).status_code == 200
        async with session_factory() as session:
            participant = await session.scalar(
                select(Participant).order_by(Participant.id.desc()).limit(1)
            )
            assert participant is not None
            assert participant.user_id is None
            assert participant.themes is None, "NULL = jamais réglé, et non « aucun »"
            participant.themes = ["sante", "logement"]
            await session.commit()
            participant_id = participant.id

        response = await register(client, "arrivee@exemple.fr")
        assert response.status_code in (200, 201), response.text

    async with session_factory() as session:
        participant = await session.get(Participant, participant_id)
        assert participant.user_id is not None, "le compte n'a pas été rattaché"
        assert participant.themes == ["sante", "logement"]


async def test_a_merge_does_not_lose_the_themes_set_before_signing_in(
    session_factory, client_factory
) -> None:
    """Le cas que la ligne unique ne couvre pas.

    Quand le compte a DÉJÀ un participant et qu'un anonyme existe dans un second
    navigateur, la connexion ne rattache pas : elle **fusionne**, et la ligne anonyme
    est supprimée. Les thèmes réglés dans ce navigateur partiraient avec elle si la
    fusion ne les reprenait pas — l'argument « c'est la même ligne » ne vaut pas ici.
    """
    from sqlalchemy import select

    from tests.conftest import PASSWORD, login, register

    # Premier navigateur : un compte, donc un participant rattaché.
    async with client_factory() as premier:
        assert (await register(premier, "deux-navigateurs@exemple.fr")).status_code in (200, 201)
        assert (await premier.get("/api/me")).status_code == 200

    # Second navigateur : un anonyme qui règle ses thèmes, puis se connecte.
    async with client_factory() as second:
        assert (await second.get("/api/me")).status_code == 200
        async with session_factory() as session:
            anonyme = await session.scalar(
                select(Participant)
                .where(Participant.user_id.is_(None))
                .order_by(Participant.id.desc())
                .limit(1)
            )
            assert anonyme is not None
            anonyme.themes = ["transports", "energie"]
            await session.commit()
            anonyme_id = anonyme.id

        assert (await login(second, "deux-navigateurs@exemple.fr", PASSWORD)).status_code == 204
        assert (await second.get("/api/me")).status_code == 200

    async with session_factory() as session:
        assert await session.get(Participant, anonyme_id) is None, (
            "la fusion aurait dû supprimer la ligne anonyme"
        )
        du_compte = await session.scalar(
            select(Participant).where(Participant.user_id.is_not(None))
        )
        assert du_compte.themes == ["transports", "energie"]


async def test_the_account_keeps_its_own_themes_over_the_anonymous_ones(
    session_factory,
) -> None:
    """Même règle que pour les votes : la cible l'emporte. Quelqu'un qui a réglé ses
    thèmes sous son compte ne les voit pas remplacés par ceux d'un navigateur de
    passage."""
    from app.services.participants import merge_into

    async with session_factory() as session:
        source = Participant(anon_token="jeton-source", themes=["sport"])
        cible = Participant(anon_token="jeton-cible", themes=["sante"])
        session.add_all([source, cible])
        await session.commit()
        compte = await merge_into(session, source, cible)
        assert compte["themes"] == 0
        assert cible.themes == ["sante"]


# --- J5 : les thèmes posés sur les débats -----------------------------------------


async def test_a_proposed_conversation_carries_the_themes_its_author_chose(
    client, session_factory
) -> None:
    from sqlalchemy import select

    reponse = await client.post(
        "/proposer",
        data={
            "title": "Le permis à seize ans",
            "description": "",
            "statements": ["Première.", "Deuxième.", "Troisième."],
            "themes": ["transports", "famille"],
        },
        follow_redirects=False,
    )
    assert reponse.status_code == 303, reponse.text

    async with session_factory() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.title == "Le permis à seize ans")
        )
        assert conversation.codes_themes == ["famille", "transports"]
        assert conversation.libelles_themes == [
            "Lien social, famille et enfance",
            "Transports et mobilités",
        ]


async def test_proposing_without_a_theme_is_allowed_because_it_is_a_suggestion(
    client, session_factory
) -> None:
    """`/proposer` ne publie pas : il dépose dans la file.

    La règle du client est « au moins un thème exigé **à la publication** », et le
    proposeur SUGGÈRE (question 5). Rendre la case obligatoire ici ajouterait une étape
    entre « je veux participer » et « je participe », ce que la question 8 écarte. C'est
    le modérateur qui ne pourra pas approuver sans thème.
    """
    from sqlalchemy import select

    reponse = await client.post(
        "/proposer",
        data={
            "title": "Sans thème",
            "description": "",
            "statements": ["Première.", "Deuxième.", "Troisième."],
        },
        follow_redirects=False,
    )
    assert reponse.status_code == 303, reponse.text

    async with session_factory() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.title == "Sans thème")
        )
        assert conversation.codes_themes == []
        assert conversation.moderation_status.value == "pending"


async def test_the_theme_error_comes_before_the_link_error(client) -> None:
    """Une case oubliée est la faute la plus probable et la plus vite corrigée.

    Signaler d'abord une adresse mal formée ferait traverser deux fois le formulaire
    pour deux fautes commises en même temps.
    """
    reponse = await client.post(
        "/proposer",
        data={
            "title": "Deux fautes à la fois",
            "description": "",
            "statements": ["Première.", "Deuxième.", "Troisième."],
            "themes": ["sante", "logement", "transports", "energie"],
            "source_url": ["pas-une-adresse", "", "", "", "", ""],
            "source_label": ["", "", "", "", "", ""],
        },
    )
    assert reponse.status_code == 200
    assert "thème" in reponse.text
    # « Proposition 1 — … » est la forme d'un message d'erreur de lien ; « Proposition 1 »
    # tout court est le repère d'un champ, et figure sur la page en toutes circonstances.
    assert "Proposition 1 —" not in reponse.text


async def test_more_than_three_themes_is_refused_even_as_a_suggestion(client) -> None:
    """Le plafond, lui, vaut partout : au-delà de trois, l'étiquetage ne trie plus
    rien, et laisser passer une suggestion à six ne ferait que reporter le refus."""
    reponse = await client.post(
        "/proposer",
        data={
            "title": "Quatre thèmes",
            "description": "",
            "statements": ["Première.", "Deuxième.", "Troisième."],
            "themes": ["sante", "logement", "transports", "energie"],
        },
    )
    assert reponse.status_code == 200
    assert "thème" in reponse.text
    # Ce qui a été tapé est réaffiché : un refus ne doit pas faire retaper le reste.
    assert "Quatre thèmes" in reponse.text
    assert "Deuxième." in reponse.text


async def test_an_invented_theme_code_is_refused(client) -> None:
    """Un code inventé vient d'un formulaire trafiqué, pas d'une case cochée. L'écarter
    en silence enregistrerait un étiquetage que personne n'a voulu."""
    reponse = await client.post(
        "/proposer",
        data={
            "title": "Code inventé",
            "description": "",
            "statements": ["Première.", "Deuxième.", "Troisième."],
            "themes": ["licornes"],
        },
    )
    assert reponse.status_code == 200
    assert "thème" in reponse.text


async def test_the_moderator_arbitrates_the_themes_when_approving(
    client, moderator_client, session_factory
) -> None:
    """Le proposeur suggère, le modérateur tranche — dans le même geste que
    l'approbation, et non dans un écran de plus."""
    from sqlalchemy import select

    await client.post(
        "/proposer",
        data={
            "title": "Mal rangée au départ",
            "description": "",
            "statements": ["Première.", "Deuxième.", "Troisième."],
            "themes": ["sport"],
        },
        follow_redirects=False,
    )
    async with session_factory() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.title == "Mal rangée au départ")
        )
        assert conversation.codes_themes == ["sport"]
        conversation_id = conversation.id

    file = (await moderator_client.get("/moderation/queue")).text
    assert "Mal rangée au départ" in file
    assert 'name="themes"' in file

    reponse = await moderator_client.post(
        f"/moderation/queue/conversations/{conversation_id}/approve",
        data={"themes": ["education", "egalite"]},
        follow_redirects=False,
    )
    assert reponse.status_code == 303, reponse.text

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation.codes_themes == ["education", "egalite"], (
            "le choix du modérateur remplace celui du proposeur, il ne s'y ajoute pas"
        )


async def test_approving_without_a_theme_is_refused(
    client, moderator_client, session_factory
) -> None:
    from sqlalchemy import select

    await client.post(
        "/proposer",
        data={
            "title": "À approuver sans thème",
            "description": "",
            "statements": ["Première.", "Deuxième.", "Troisième."],
            "themes": ["sport"],
        },
        follow_redirects=False,
    )
    async with session_factory() as session:
        conversation_id = (
            await session.scalar(
                select(Conversation).where(
                    Conversation.title == "À approuver sans thème"
                )
            )
        ).id

    refus = await moderator_client.post(
        f"/moderation/queue/conversations/{conversation_id}/approve",
        data={},
        follow_redirects=False,
    )
    assert refus.status_code == 422

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation.moderation_status.value == "pending", (
            "un refus de validation ne doit pas avoir approuvé au passage"
        )


async def test_rejecting_needs_no_theme(client, moderator_client, session_factory) -> None:
    """Il n'y a rien à ranger dans une conversation qui ne sera pas publiée."""
    from sqlalchemy import select

    await client.post(
        "/proposer",
        data={
            "title": "À rejeter",
            "description": "",
            "statements": ["Première.", "Deuxième.", "Troisième."],
            "themes": ["sport"],
        },
        follow_redirects=False,
    )
    async with session_factory() as session:
        conversation_id = (
            await session.scalar(
                select(Conversation).where(Conversation.title == "À rejeter")
            )
        ).id

    reponse = await moderator_client.post(
        f"/moderation/queue/conversations/{conversation_id}/reject",
        data={},
        follow_redirects=False,
    )
    assert reponse.status_code == 303, reponse.text


async def test_a_draft_may_carry_no_theme_but_a_published_debate_may_not(
    moderator_client, session_factory
) -> None:
    """La règle porte sur la PUBLICATION, pas sur l'existence.

    Un brouillon n'est visible de personne et rien ne le filtre encore ; l'exiger là
    empêcherait de préparer un débat avant d'avoir arrêté son sujet.
    """
    from tests.conftest import create_conversation

    conversation_id = await create_conversation(moderator_client, title="En préparation")

    brouillon = await moderator_client.post(
        f"/moderation/conversations/{conversation_id}",
        data={
            "title": "En préparation",
            "description": "",
            "state": "draft",
            "moderation_mode": "pre",
        },
        follow_redirects=False,
    )
    assert brouillon.status_code == 303, brouillon.text

    publication = await moderator_client.post(
        f"/moderation/conversations/{conversation_id}",
        data={
            "title": "En préparation",
            "description": "",
            "state": "open",
            "moderation_mode": "pre",
        },
        follow_redirects=False,
    )
    assert publication.status_code == 422

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation.state.value == "draft", (
            "le refus ne doit pas avoir publié au passage"
        )


async def test_an_already_published_debate_can_be_tagged_in_one_save(
    moderator_client, session_factory
) -> None:
    """Le cas de la reprise des débats existants, qui aurait pu être un piège.

    La règle porte sur ce qui est SOUMIS et non sur ce qui est stocké. Sans cela, un
    débat déjà en ligne et sans thème n'aurait pas pu être étiqueté : l'enregistrement
    qui ajoute le thème aurait été refusé pour absence de thème.
    """
    from tests.conftest import open_conversation

    conversation_id, _, _ = await open_conversation(
        moderator_client, statements=1, title="Déjà en ligne"
    )
    # L'état d'un débat antérieur au J5 : publié, et sans aucune étiquette. Il ne peut
    # pas être obtenu par les écrans — c'est justement ce qu'ils refusent désormais —
    # donc il est reconstitué en base, comme la migration l'a laissé.
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        conversation.themes = []
        await session.commit()
        assert conversation.state.value == "open"
        assert conversation.codes_themes == []

    reponse = await moderator_client.post(
        f"/moderation/conversations/{conversation_id}",
        data={
            "title": "Déjà en ligne",
            "description": "",
            "state": "open",
            "moderation_mode": "pre",
            "themes": ["transports"],
        },
        follow_redirects=False,
    )
    assert reponse.status_code == 303, reponse.text

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation.codes_themes == ["transports"]


async def test_the_labels_show_on_the_debate_and_the_list_but_no_longer_on_the_home(
    client, moderator_client, session_factory
) -> None:
    """Les étiquettes de thème ont quitté les cartes de l'ACCUEIL au chantier I2
    (instruction 4) : la maquette ne montre qu'un ou deux badges de contexte par carte,
    jamais une liste de thèmes.

    Le test est **relu** plutôt que supprimé, comme le J6 l'avait fait pour les liens :
    il dit maintenant que les étiquettes sont là où elles servent — la page d'un débat
    et la page « Débats », où l'on vient chercher un débat précis — et qu'elles ne sont
    plus sur l'accueil. Le thème lui-même n'a pas bougé d'un pouce en base.
    """
    from tests.conftest import open_conversation

    conversation_id, slug, _ = await open_conversation(
        moderator_client, statements=1, title="Un débat étiqueté"
    )
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        conversation.themes = [
            ConversationTheme(code="transports"),
            ConversationTheme(code="territoire"),
        ]
        await session.commit()

    accueil = (await client.get("/")).text
    assert "etiquette--theme" not in accueil, "les cartes de l'accueil ne portent plus de thème"
    assert "Transports et mobilités" not in accueil
    assert "Territoire, ruralité et outre-mer" not in accueil

    page = (await client.get(f"/c/{slug}")).text
    assert "Transports et mobilités" in page

    debats = (await client.get("/debats")).text
    assert "Transports et mobilités" in debats
    # Étiquette NEUTRE : le jaune reste réservé à « Nouveau ».
    assert "etiquette--theme" in debats


async def test_the_labels_became_links_at_j6(
    client, moderator_client, session_factory
) -> None:
    """Ce test disait au J5 qu'aucune étiquette n'était un lien — le filtre n'existait
    pas encore, et une étiquette cliquable qui n'aurait rien filtré aurait été pire que
    pas de lien du tout.

    Le J6 apporte le filtre, et le test est **relu** plutôt que supprimé : il dit
    maintenant l'inverse, et il dit surtout vers quoi le lien mène.

    Relu une seconde fois au chantier I2 (instruction 4) : les cartes de l'accueil ne
    portent plus d'étiquette de thème du tout, donc plus de lien de filtre non plus.
    C'est sur la page « Débats » que le lien se vérifie désormais.
    """
    from tests.conftest import open_conversation

    conversation_id, _, _ = await open_conversation(
        moderator_client, statements=1, title="Cliquable"
    )
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        conversation.themes = [ConversationTheme(code="transports")]
        await session.commit()

    accueil = (await client.get("/")).text
    assert '/?theme=transports' not in accueil, "plus d'étiquette de thème sur l'accueil"
    # Depuis la page « Débats », l'étiquette filtre la page « Débats » : une macro
    # rendue par trois écrans reçoit sa base en argument, elle ne la devine pas.
    debats = (await client.get("/debats")).text
    assert '/debats?theme=transports' in debats

    # Le filtre lui-même n'a pas bougé : l'adresse continue de le porter, on y arrive
    # maintenant par la page « Débats » plutôt que par une carte de l'accueil.
    filtre = (await client.get("/debats?theme=transports")).text
    assert "Cliquable" in filtre


async def test_saving_the_same_themes_twice_does_not_break(
    moderator_client, session_factory
) -> None:
    """Le défaut que seul un second enregistrement révèle.

    Réaffecter la collection en bloc laisse SQLAlchemy émettre l'INSERT de la ligne
    neuve avant le DELETE de l'ancienne dans un même vidage, et `uq_conversation_theme`
    refuse le doublon. Aucun test qui n'enregistre qu'une fois ne peut le voir — et un
    modérateur qui ouvre puis clôt une conversation le rencontre à tous les coups.
    """
    from tests.conftest import open_conversation

    conversation_id, _, _ = await open_conversation(
        moderator_client, statements=1, title="Enregistrée deux fois"
    )
    for etat in ("open", "closed", "open"):
        reponse = await moderator_client.post(
            f"/moderation/conversations/{conversation_id}",
            data={
                "title": "Enregistrée deux fois",
                "description": "",
                "state": etat,
                "moderation_mode": "pre",
                "themes": ["institutions"],
            },
            follow_redirects=False,
        )
        assert reponse.status_code == 303, reponse.text

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation.codes_themes == ["institutions"]


async def test_changing_the_themes_keeps_only_the_new_ones(
    moderator_client, session_factory
) -> None:
    """Un thème conservé d'un enregistrement à l'autre garde sa ligne ; les autres
    partent. C'est ce qui permet de changer un thème sur trois sans tout réécrire."""
    from tests.conftest import open_conversation

    conversation_id, _, _ = await open_conversation(
        moderator_client, statements=1, title="Étiquetage revu"
    )
    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}",
        data={
            "title": "Étiquetage revu",
            "description": "",
            "state": "open",
            "moderation_mode": "pre",
            "themes": ["institutions", "sante", "logement"],
        },
        follow_redirects=False,
    )
    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}",
        data={
            "title": "Étiquetage revu",
            "description": "",
            "state": "open",
            "moderation_mode": "pre",
            "themes": ["institutions", "transports"],
        },
        follow_redirects=False,
    )
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        # L'ordre rendu est celui de la LISTE (transports 9e, institutions 13e), pas
        # celui de la saisie ni celui des lignes conservées.
        assert conversation.codes_themes == ["transports", "institutions"]
