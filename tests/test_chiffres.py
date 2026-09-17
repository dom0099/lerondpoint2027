"""Les chiffres publics d'un débat : la page, et l'écran qui l'alimente (J3 et J4).

Distinct de `test_sources.py`, qui garde les liens des participants. Les deux mécaniques
portent une URL et s'arrêtent là : elles n'ont ni le même auteur, ni le même rythme, ni
le même risque — et depuis la décision du client, elles ne partagent même plus de page.
"""

import datetime as dt

import pytest

from app.models import Conversation, ConversationSource
from app.services.chiffres import (
    date_saisie,
    en_toutes_lettres,
    pourcentage,
    valider_chiffre,
)


# --- ce que le service tient, avant tout écran ------------------------------------


def test_a_date_reads_as_words_and_not_as_digits() -> None:
    """« 01/2024 » et « 2024-01-01 » demandent tous deux un effort que « janvier 2024 »
    n'exige pas — sur une page dont l'objet est de dissiper une confusion de dates."""
    assert en_toutes_lettres(dt.date(2024, 3, 12)) == "12 mars 2024"
    assert en_toutes_lettres(dt.date(2026, 8, 1)) == "1 août 2026"
    assert en_toutes_lettres(None) is None


def test_an_unreadable_date_is_treated_as_absent_not_refused() -> None:
    """Le champ est facultatif, et un navigateur sans `type="date"` laisse passer du
    texte libre. Refuser ferait perdre le reste du formulaire pour un champ dont
    l'absence est prévue."""
    assert date_saisie("2024-03-12") == dt.date(2024, 3, 12)
    assert date_saisie("") is None
    assert date_saisie("n'importe quoi") is None
    assert date_saisie(None) is None


def test_a_figure_without_a_source_is_not_a_figure() -> None:
    with pytest.raises(ValueError):
        valider_chiffre("Un intitulé", "12,4 %", "")


# --- le graphique en gaufre (chantier I) tient le rang : rien qui n'est pas ------
# vraiment un pourcentage n'obtient de graphique. -----------------------------------


def test_the_written_forms_a_moderator_actually_uses_are_recognised() -> None:
    """« % », « pour cent » et le pluriel fautif « pourcents » — cette dernière forme
    est celle de plusieurs chiffres déjà en base, relevés avant ce chantier."""
    assert pourcentage("40 %") == 40
    assert pourcentage("40%") == 40
    assert pourcentage("40 pour cent") == 40
    assert pourcentage("40 pourcents") == 40
    assert pourcentage("0 %") == 0
    assert pourcentage("100 %") == 100


def test_anything_that_is_not_a_percentage_gets_no_chart() -> None:
    """Le champ est du texte libre : « 1 284 », un montant, une phrase. Un chiffre qui
    n'est pas un pourcentage reste affiché en grand, sans graphique inventé."""
    assert pourcentage("1 284") is None
    assert pourcentage("35 €") is None
    assert pourcentage("101 %") is None
    assert pourcentage("environ 40 %") is None


def test_a_moderator_is_not_above_the_url_rules() -> None:
    """Un chiffre officiel accompagné d'un `javascript:` reste un `javascript:`, et un
    modérateur n'est pas plus à l'abri d'un copier-coller malheureux qu'un visiteur."""
    with pytest.raises(ValueError):
        valider_chiffre("Un intitulé", "12,4 %", "javascript:alert(1)")
    with pytest.raises(ValueError):
        valider_chiffre("Un intitulé", "12,4 %", "www.insee.fr/x")


def test_the_title_and_the_figure_are_both_required() -> None:
    with pytest.raises(ValueError):
        valider_chiffre("", "12,4 %", "https://insee.fr/x")
    with pytest.raises(ValueError):
        valider_chiffre("Un intitulé", "", "https://insee.fr/x")


# --- la page publique (J3) ---------------------------------------------------------


async def _debat_avec_chiffres(moderator_client, session_factory, chiffres):
    """Un débat publié et ses chiffres.

    Les lignes sont ajoutées par leur `conversation_id`, comme le fait l'application :
    affecter `conversation.sources` déclencherait un chargement paresseux de la
    collection, qui lève `MissingGreenlet` en asynchrone. La relation reste volontairement
    paresseuse — l'accueil charge cinq conversations et n'a que faire de leurs chiffres.
    """
    from tests.conftest import open_conversation

    conversation_id, slug, _ = await open_conversation(
        moderator_client, statements=1, title="Débat chiffré"
    )
    async with session_factory() as session:
        for chiffre in chiffres:
            chiffre.conversation_id = conversation_id
            session.add(chiffre)
        await session.commit()
    return conversation_id, slug


async def test_the_debate_page_leads_with_at_most_two_figures(
    client, moderator_client, session_factory
) -> None:
    """Le bandeau du haut de la page d'un débat (chantier I2, instruction 9).

    Il montre au plus DEUX chiffres — les deux premiers dans l'ordre du modérateur —,
    nomme le domaine réel de leur source, et renvoie à la page complète pour le reste.
    Trois chiffres en base, deux à l'écran : c'est le lien qui porte les autres.
    """
    _, slug = await _debat_avec_chiffres(
        moderator_client,
        session_factory,
        [
            ConversationSource(
                position=1, titre="Le premier", valeur="40 %",
                url="https://www.securite-routiere.gouv.fr/a",
            ),
            ConversationSource(
                position=2, titre="Le deuxième", valeur="11 %",
                url="https://www.securite-routiere.gouv.fr/b",
            ),
            ConversationSource(
                position=3, titre="Le troisième", valeur="7 %",
                url="https://www.insee.fr/c",
            ),
        ],
    )
    page = (await client.get(f"/c/{slug}")).text
    assert "Le premier" in page and "Le deuxième" in page
    assert "Le troisième" not in page, "le bandeau s'arrête à deux, le lien porte le reste"
    assert f"/c/{slug}/chiffres" in page
    # Le domaine RÉEL des chiffres montrés, pas un nom de source inventé.
    assert "securite-routiere.gouv.fr" in page
    assert "relevés à la main par la modération" in page
    # Le troisième chiffre vient d'ailleurs, mais il n'est pas montré : son domaine non
    # plus. L'attribution ne parle que de ce qui est à l'écran.
    assert "insee.fr" not in page
    # Un seul chiffre affiché ne demande pas de séparateur ; deux, si.
    assert page.count("separateur-chiffres") == 1


async def test_a_debate_without_figures_shows_no_band_at_all(
    client, moderator_client, session_factory
) -> None:
    """Pas de bandeau vide, et pas de lien vers une page de chiffres qui n'en a aucun :
    l'un et l'autre promettraient ce qu'ils ne montrent pas."""
    from tests.conftest import open_conversation

    _, slug, _ = await open_conversation(
        moderator_client, statements=1, title="Débat sans chiffre"
    )
    page = (await client.get(f"/c/{slug}")).text
    assert "bandeau-chiffres" not in page
    assert f"/c/{slug}/chiffres" not in page


async def test_the_debate_head_counts_people_not_votes(
    client, moderator_client, session_factory
) -> None:
    """« N participants » compte des PERSONNES : quelqu'un qui répond à trois
    propositions compte pour un.

    À ne pas confondre avec `GroupView.total_participants`, qui ne compte que les
    participants du dernier calcul de groupes — inexistant tant qu'aucun calcul n'a
    abouti, alors que le bandeau doit dire vrai dès le premier vote.
    """
    from app.models import Conversation
    from app.services import votes as votes_service
    from tests.conftest import open_conversation

    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=3, title="Débat compté"
    )
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert await votes_service.participants_de(session, conversation) == 0

    # Une seule personne, trois votes.
    for statement_id in statements:
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement_id, "value": 1},
        )

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert await votes_service.participants_de(session, conversation) == 1, (
            "trois votes d'une même personne font un participant, pas trois"
        )

    # Et c'est bien ce 1 que le bandeau affiche, pas le nombre de votes.
    page = (await client.get(f"/c/{slug}")).text
    assert "<b>1</b>participant" in page
    assert "<b>3</b>proposition" in page


async def test_the_page_shows_the_figure_its_source_and_BOTH_dates(
    client, moderator_client, session_factory
) -> None:
    _, slug = await _debat_avec_chiffres(
        moderator_client,
        session_factory,
        [
            ConversationSource(
                position=1,
                titre="Communes de moins de 500 habitants",
                valeur="3 200 communes",
                url="https://www.insee.fr/fr/statistiques/1",
                date_donnees=dt.date(2024, 1, 1),
                date_verification=dt.date(2026, 9, 5),
            )
        ],
    )
    page = (await client.get(f"/c/{slug}/chiffres")).text
    assert "3 200 communes" in page
    assert "Communes de moins de 500 habitants" in page
    assert "insee.fr" in page
    assert "1 janvier 2024" in page
    assert "5 septembre 2026" in page


async def test_a_missing_date_is_NAMED_and_not_passed_over_in_silence(
    client, moderator_client, session_factory
) -> None:
    """Le cœur de la décision « deux dates et non une ».

    Un chiffre dont seule la vérification est datée afficherait « vérifié le 5
    septembre » — et le lecteur en conclurait que la donnée est de septembre. Dire que
    la date de la donnée manque est moins flatteur, et c'est la seule chose honnête.
    """
    _, slug = await _debat_avec_chiffres(
        moderator_client,
        session_factory,
        [
            ConversationSource(
                titre="Un chiffre sans millésime",
                valeur="12,4 %",
                url="https://insee.fr/x",
                date_verification=dt.date(2026, 9, 5),
            )
        ],
    )
    page = (await client.get(f"/c/{slug}/chiffres")).text
    assert "Date de la donnée non précisée" in page
    assert "5 septembre 2026" in page


async def test_the_figures_follow_the_order_the_moderator_gave(
    client, moderator_client, session_factory
) -> None:
    _, slug = await _debat_avec_chiffres(
        moderator_client,
        session_factory,
        [
            ConversationSource(position=2, titre="Second", valeur="B", url="https://a.fr"),
            ConversationSource(position=1, titre="Premier", valeur="A", url="https://b.fr"),
        ],
    )
    page = (await client.get(f"/c/{slug}/chiffres")).text
    assert page.index("Premier") < page.index("Second")


async def test_a_participant_link_never_appears_on_this_page(
    client, moderator_client, session_factory
) -> None:
    """La décision du client, éprouvée plutôt que supposée : cette page ne porte QUE
    les chiffres du modérateur. Les liens des participants restent sous la proposition
    qu'ils appuient."""
    conversation_id, slug = await _debat_avec_chiffres(
        moderator_client,
        session_factory,
        [ConversationSource(titre="Un chiffre", valeur="1", url="https://insee.fr/z")],
    )
    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}/statements",
        data={
            "text": "Une amorce sourcée.",
            "source_url": "https://apport-de-participant.example/page",
            "source_label": "Mon lien",
        },
        follow_redirects=False,
    )
    page = (await client.get(f"/c/{slug}/chiffres")).text
    assert "apport-de-participant.example" not in page
    assert "Mon lien" not in page
    assert "Un chiffre" in page


async def test_the_link_to_the_page_appears_only_once_there_is_something_to_show(
    client, moderator_client, session_factory
) -> None:
    """Un lien vers une page vide promet ce qu'elle ne montre pas."""
    from tests.conftest import open_conversation

    conversation_id, slug, _ = await open_conversation(
        moderator_client, statements=1, title="Sans chiffre"
    )
    assert "/chiffres" not in (await client.get(f"/c/{slug}")).text

    async with session_factory() as session:
        session.add(
            ConversationSource(
                conversation_id=conversation_id,
                titre="Un chiffre",
                valeur="1",
                url="https://insee.fr/z",
            )
        )
        await session.commit()

    assert f"/c/{slug}/chiffres" in (await client.get(f"/c/{slug}")).text


async def test_the_page_of_an_unpublished_debate_is_a_404(
    client, moderator_client
) -> None:
    from tests.conftest import create_conversation

    await create_conversation(moderator_client, title="Un brouillon")
    assert (await client.get("/c/un-brouillon/chiffres")).status_code == 404


async def test_the_route_is_not_swallowed_by_the_debate_route(
    client, moderator_client, session_factory
) -> None:
    """`/c/{slug}` ne capture pas la barre oblique, mais l'ORDRE de déclaration des
    routes décide tout de même laquelle répond. Le test existe pour que réordonner le
    fichier ne casse pas la page en silence."""
    _, slug = await _debat_avec_chiffres(
        moderator_client,
        session_factory,
        [ConversationSource(titre="Un chiffre", valeur="1", url="https://insee.fr/z")],
    )
    reponse = await client.get(f"/c/{slug}/chiffres")
    assert reponse.status_code == 200
    assert "Les chiffres du débat" in reponse.text


# --- l'écran de saisie (J4) --------------------------------------------------------


async def test_a_moderator_adds_edits_reorders_and_deletes(
    moderator_client, session_factory
) -> None:
    from sqlalchemy import select

    from tests.conftest import open_conversation

    conversation_id, _, _ = await open_conversation(
        moderator_client, statements=1, title="À chiffrer"
    )
    base = f"/moderation/conversations/{conversation_id}/chiffres"

    ajout = await moderator_client.post(
        base,
        data={
            "titre": "Taux d'effort",
            "valeur": "12,4 %",
            "url": "https://www.insee.fr/fr/statistiques/2",
            "date_donnees": "2024-01-01",
            "date_verification": "2026-09-05",
            "position": "1",
        },
        follow_redirects=False,
    )
    assert ajout.status_code == 303, ajout.text

    async with session_factory() as session:
        chiffre = await session.scalar(
            select(ConversationSource).where(
                ConversationSource.conversation_id == conversation_id
            )
        )
        assert chiffre.valeur == "12,4 %"
        assert chiffre.date_donnees == dt.date(2024, 1, 1)
        source_id = chiffre.id

    modification = await moderator_client.post(
        f"{base}/{source_id}",
        data={
            "titre": "Taux d'effort des locataires",
            "valeur": "13,1 %",
            "url": "https://www.insee.fr/fr/statistiques/2",
            "date_donnees": "2025-01-01",
            "date_verification": "",
            "position": "5",
        },
        follow_redirects=False,
    )
    assert modification.status_code == 303

    async with session_factory() as session:
        chiffre = await session.get(ConversationSource, source_id)
        assert chiffre.valeur == "13,1 %"
        assert chiffre.position == 5
        # Une date vidée est bien effacée, et non conservée par inadvertance.
        assert chiffre.date_verification is None

    suppression = await moderator_client.post(
        f"{base}/{source_id}/supprimer", follow_redirects=False
    )
    assert suppression.status_code == 303

    async with session_factory() as session:
        assert await session.get(ConversationSource, source_id) is None


async def test_a_figure_cannot_be_edited_through_another_debate(
    moderator_client, session_factory
) -> None:
    """Une route qui accepte deux identifiants doit vérifier qu'ils vont ensemble —
    sinon un lien recopié de travers écrit dans le mauvais débat."""
    from sqlalchemy import select

    from tests.conftest import open_conversation

    premier, _, _ = await open_conversation(moderator_client, statements=1, title="Premier débat")
    second, _, _ = await open_conversation(moderator_client, statements=1, title="Second débat")

    await moderator_client.post(
        f"/moderation/conversations/{premier}/chiffres",
        data={"titre": "Un chiffre", "valeur": "1", "url": "https://insee.fr/z"},
        follow_redirects=False,
    )
    async with session_factory() as session:
        source_id = (
            await session.scalar(
                select(ConversationSource).where(
                    ConversationSource.conversation_id == premier
                )
            )
        ).id

    usurpation = await moderator_client.post(
        f"/moderation/conversations/{second}/chiffres/{source_id}",
        data={"titre": "Détourné", "valeur": "0", "url": "https://ailleurs.example/x"},
        follow_redirects=False,
    )
    assert usurpation.status_code == 404

    effacement = await moderator_client.post(
        f"/moderation/conversations/{second}/chiffres/{source_id}/supprimer",
        follow_redirects=False,
    )
    assert effacement.status_code == 404

    async with session_factory() as session:
        assert (await session.get(ConversationSource, source_id)).titre == "Un chiffre"


async def test_a_bad_address_is_refused_with_a_message(moderator_client) -> None:
    from tests.conftest import open_conversation

    conversation_id, _, _ = await open_conversation(
        moderator_client, statements=1, title="Adresse fautive"
    )
    refus = await moderator_client.post(
        f"/moderation/conversations/{conversation_id}/chiffres",
        data={"titre": "Un chiffre", "valeur": "1", "url": "javascript:alert(1)"},
        follow_redirects=False,
    )
    assert refus.status_code == 422
    assert "http" in refus.json()["detail"]


async def test_the_editor_lists_the_figures(moderator_client, session_factory) -> None:
    conversation_id, _ = await _debat_avec_chiffres(
        moderator_client,
        session_factory,
        [ConversationSource(titre="Communes rurales", valeur="3 200", url="https://insee.fr/z")],
    )
    page = (
        await moderator_client.get(f"/moderation/conversations/{conversation_id}")
    ).text
    assert "Communes rurales" in page
    assert "3 200" in page
    assert "Supprimer ce chiffre" in page


async def test_deleting_a_debate_takes_its_figures_with_it(
    moderator_client, session_factory
) -> None:
    from sqlalchemy import func, select

    conversation_id, _ = await _debat_avec_chiffres(
        moderator_client,
        session_factory,
        [ConversationSource(titre="Un chiffre", valeur="1", url="https://insee.fr/z")],
    )
    async with session_factory() as session:
        await session.delete(await session.get(Conversation, conversation_id))
        await session.commit()

    async with session_factory() as session:
        restants = await session.scalar(
            select(func.count(ConversationSource.id)).where(
                ConversationSource.conversation_id == conversation_id
            )
        )
        assert restants == 0
