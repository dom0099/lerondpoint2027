"""Le filtre par thème, et les centres d'intérêt d'un visiteur (chantiers J6 et J7).

Un fichier à part de `test_themes.py`, qui garde la liste et l'étiquetage : ce qui est
éprouvé ici est la LISTE FILTRÉE — comment le filtre et le tri cohabitent, ce que le
compteur annonce, et ce qu'un visiteur peut régler.
"""

import pytest

from app.models import Conversation, ConversationTheme, Participant
from app.services.accueil import Filtre, adresse, filtre_valide


async def _debat(moderator_client, session_factory, titre, codes, statements=1):
    from tests.conftest import open_conversation

    conversation_id, slug, _ = await open_conversation(
        moderator_client, statements=statements, title=titre
    )
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        conversation.themes = [ConversationTheme(code=code) for code in codes]
        await session.commit()
    return conversation_id, slug


# --- ce que le filtre est, avant tout écran ---------------------------------------


def test_an_unknown_theme_falls_back_to_the_whole_list() -> None:
    """Même règle que `tri_valide` : une adresse recopiée avec une faute de frappe doit
    afficher la page, pas un 422."""
    assert filtre_valide("licornes", False, None).actif is False
    assert filtre_valide(None, False, None).actif is False


def test_the_two_filters_never_apply_together() -> None:
    """Nos liens n'émettent jamais les deux, mais une URL recopiée le peut. `theme`
    l'emporte : c'est le geste le plus explicite des deux."""
    filtre = filtre_valide("sante", True, ["transports", "energie"])
    assert filtre.codes == ("sante",)
    assert filtre.sur_mes_themes is False


def test_my_themes_without_any_preference_filters_nothing() -> None:
    """Un `mes-themes=1` recopié par quelqu'un qui n'a rien réglé montre la liste
    entière — et non une liste vide, qui se lirait comme une panne."""
    assert filtre_valide(None, True, None).actif is False
    assert filtre_valide(None, True, []).actif is False


def test_an_address_keeps_what_it_is_not_asked_to_change() -> None:
    """C'est la fonction qui empêche « un clic sur *Le plus voté* efface le filtre »."""
    filtre = Filtre(theme="sante")
    assert adresse("/", tri="votes", filtre=filtre) == "/?tri=votes&theme=sante"
    # Poser un thème retire « mes thèmes », et l'inverse : les deux sont exclusifs, et
    # une adresse qui porterait les deux aurait un effet réglé par une priorité que
    # personne ne lit.
    assert "theme=sante" not in adresse("/", tri="recent", filtre=filtre, mes_themes=1)
    assert adresse("/", tri="recent", filtre=filtre, theme=None) == "/?tri=recent"


# --- le filtre à l'écran -----------------------------------------------------------


async def test_a_theme_filter_reduces_the_list_and_says_how_to_leave(
    client, moderator_client, session_factory
) -> None:
    await _debat(moderator_client, session_factory, "Le train", ["transports"])
    await _debat(moderator_client, session_factory, "La santé", ["sante"])

    entier = (await client.get("/")).text
    assert "Le train" in entier and "La santé" in entier

    filtre = (await client.get("/?tri=recent&theme=transports")).text
    assert "Le train" in filtre
    assert "La santé" not in filtre
    # Le lecteur voit ce qu'il ne voit pas, et en sort d'un clic.
    assert "Transports et mobilités" in filtre
    assert "voir tous les débats" in filtre.lower()


async def test_the_sort_keeps_the_filter_and_the_filter_keeps_the_sort(
    client, moderator_client, session_factory
) -> None:
    """Le défaut que le J0 avait nommé, éprouvé dans les deux sens.

    Le tri ORDONNE, le filtre RESTREINT : ce sont deux questions, et répondre à l'une
    ne doit pas effacer la réponse à l'autre.
    """
    await _debat(moderator_client, session_factory, "Le train", ["transports"])

    page = (await client.get("/?tri=recent&theme=transports")).text
    # Les onglets de tri conservent le thème...
    assert "tri=votes&amp;theme=transports" in page or "tri=votes&theme=transports" in page

    page_votes = (await client.get("/?tri=votes&theme=transports")).text
    # ...et le tri courant survit au chargement de la page filtrée.
    assert 'aria-current="page"' in page_votes
    assert "tri=recent&amp;theme=transports" in page_votes or "tri=recent&theme=transports" in page_votes


async def test_the_counter_follows_the_filter(
    client, moderator_client, session_factory
) -> None:
    """« 128 propositions ouvertes » au-dessus de trois débats filtrés est une
    contradiction visible à l'œil nu, et c'est le genre de détail qui fait douter du
    reste de la page.

    Le compteur a quitté l'ACCUEIL au chantier I2 (instruction 8) : il y faisait doublon
    avec « Le site en chiffres », plus bas sur la même page. Il reste sur la page
    « Débats », qui n'a pas cette section — c'est donc là que la règle se vérifie
    maintenant. Le test est relu, pas supprimé : la règle, elle, n'a pas changé.
    """
    import re

    await _debat(moderator_client, session_factory, "Le train", ["transports"], statements=2)
    await _debat(moderator_client, session_factory, "La santé", ["sante"], statements=5)

    def compte(html: str) -> int:
        trouve = re.search(r'class="chiffre">(\d+)</span>\s*proposition', html)
        assert trouve, "compteur introuvable"
        return int(trouve.group(1))

    assert compte((await client.get("/debats")).text) == 7
    assert compte((await client.get("/debats?theme=transports")).text) == 2
    assert compte((await client.get("/debats?theme=sante")).text) == 5

    # Et il n'est plus au-dessus de la liste de l'accueil, où il faisait doublon.
    assert "compte-propositions" not in (await client.get("/")).text

    # En revanche « Le site en chiffres » NE suit PAS le filtre, et c'est le point : il
    # annonce le site, pas la sélection courante. Il affiche donc les 7 propositions
    # dans les trois cas, filtre actif ou non.
    def compte_du_site(html: str) -> int:
        trouve = re.search(r'class="chiffre-grand">(\d+)</span>\s*<span>proposition', html)
        assert trouve, "chiffre du site introuvable"
        return int(trouve.group(1))

    for adresse in ("/", "/?theme=transports", "/?theme=sante"):
        assert compte_du_site((await client.get(adresse)).text) == 7, adresse


async def test_an_empty_filter_says_so_and_offers_the_way_out(
    client, moderator_client, session_factory
) -> None:
    await _debat(moderator_client, session_factory, "Le train", ["transports"])

    page = (await client.get("/?theme=culture")).text
    assert "Aucun débat sur" in page
    assert "/proposer" in page
    assert "Voir tous les débats" in page


async def test_the_debates_page_filters_too_and_so_do_its_batches(
    client, moderator_client, session_factory
) -> None:
    """Sans cela, descendre dans une liste filtrée la ferait déborder de débats hors
    filtre à partir de la onzième ligne."""
    for index in range(12):
        await _debat(
            moderator_client, session_factory, f"Transport n°{index}", ["transports"]
        )
    await _debat(moderator_client, session_factory, "La santé", ["sante"])

    page = (await client.get("/debats?theme=transports")).text
    assert "La santé" not in page

    fournee = (
        await client.get("/debats/suite?tri=recent&theme=transports&decalage=10")
    ).json()
    assert "La santé" not in fournee["html"]
    assert fournee["nombre"] == 2


async def test_the_home_link_to_all_debates_carries_the_filter(
    client, moderator_client, session_factory
) -> None:
    """Quelqu'un qui regarde ses thèmes classés par votes veut la suite de cela, pas le
    début d'autre chose."""
    for index in range(7):
        await _debat(
            moderator_client, session_factory, f"Transport n°{index}", ["transports"]
        )
    page = (await client.get("/?tri=votes&theme=transports")).text
    assert "/debats?tri=votes&amp;theme=transports" in page


# --- les centres d'intérêt d'un visiteur (J7) --------------------------------------


async def test_the_third_tab_appears_only_once_themes_are_set(client) -> None:
    """Un onglet qui ne ferait que reprocher de ne pas l'avoir rempli est une impasse.
    À sa place, un lien vers le réglage."""
    vierge = (await client.get("/")).text
    assert "Mes centres d'intérêt" not in vierge
    assert "Choisir mes centres d'intérêt" in vierge

    reponse = await client.post(
        "/mes-themes", data={"themes": ["transports"], "retour": "/"},
        follow_redirects=False,
    )
    assert reponse.status_code == 303

    regle = (await client.get("/")).text
    assert "Mes centres d'intérêt" in regle


async def test_an_anonymous_visitor_can_set_their_themes(
    client, session_factory
) -> None:
    """Question 9 : les visiteurs sans compte sont la majorité sur un site qui n'en
    exige pas. Une préférence réservée aux inscrits n'aurait servi presque personne."""
    from sqlalchemy import select

    reponse = await client.post(
        "/mes-themes",
        data={"themes": ["transports", "sante"], "retour": "/debats"},
        follow_redirects=False,
    )
    assert reponse.status_code == 303
    assert reponse.headers["location"] == "/debats"

    async with session_factory() as session:
        participant = await session.scalar(
            select(Participant).order_by(Participant.id.desc()).limit(1)
        )
        assert participant.user_id is None
        assert sorted(participant.themes) == ["sante", "transports"]


async def test_personal_interests_have_no_maximum(client, session_factory) -> None:
    """La demande du client, chantier profil : contrairement à l'étiquetage d'un
    débat (`valider_choix`) ou à sa suggestion à la proposition (`valider_suggestion`),
    les centres d'intérêt personnels passent par `valider_interets`, sans plafond."""
    from sqlalchemy import select

    codes = [
        "sante", "logement", "transports", "energie", "sport",
        "justice", "institutions", "international",
    ]
    reponse = await client.post(
        "/mes-themes", data={"themes": codes, "retour": "/"},
        follow_redirects=False,
    )
    assert reponse.status_code == 303, reponse.text

    async with session_factory() as session:
        participant = await session.scalar(
            select(Participant).order_by(Participant.id.desc()).limit(1)
        )
        assert sorted(participant.themes) == sorted(codes)


async def test_the_settings_page_no_longer_advertises_a_maximum(client) -> None:
    """Le plafond a disparu du réglage : le texte qui l'annonçait doit disparaître
    avec lui, sous peine de mentir sur ce que l'écran accepte réellement."""
    page = (await client.get("/mes-themes")).text
    assert "Jusqu'à" not in page


async def test_reading_the_settings_page_creates_no_participant(
    client, session_factory
) -> None:
    """Quelqu'un qui ouvre la page et repart sans cocher ne doit pas laisser une ligne
    derrière lui."""
    from sqlalchemy import func, select

    async with session_factory() as session:
        avant = await session.scalar(select(func.count(Participant.id)))

    assert (await client.get("/mes-themes")).status_code == 200

    async with session_factory() as session:
        assert await session.scalar(select(func.count(Participant.id))) == avant


async def test_the_tab_shows_the_union_of_the_chosen_themes(
    client, moderator_client, session_factory
) -> None:
    """C'est ce que l'onglet apporte de plus qu'une étiquette : trois thèmes cochés se
    voient ENSEMBLE, là où une étiquette n'en filtre qu'un."""
    await _debat(moderator_client, session_factory, "Le train", ["transports"])
    await _debat(moderator_client, session_factory, "La santé", ["sante"])
    await _debat(moderator_client, session_factory, "Les écoles", ["education"])

    await client.post(
        "/mes-themes", data={"themes": ["transports", "sante"], "retour": "/"},
        follow_redirects=False,
    )
    page = (await client.get("/?mes-themes=1")).text
    assert "Le train" in page
    assert "La santé" in page
    assert "Les écoles" not in page


async def test_choosing_none_is_an_answer_and_not_an_absence(
    client, session_factory
) -> None:
    """`NULL` = jamais réglé ; `[]` = a répondu « aucun ». Sans cette distinction,
    quelqu'un qui décoche tout se verrait reposer la question à chaque inscription."""
    from sqlalchemy import select

    await client.post("/mes-themes", data={"themes": ["sante"], "retour": "/"},
                      follow_redirects=False)
    await client.post("/mes-themes", data={"retour": "/"}, follow_redirects=False)

    async with session_factory() as session:
        participant = await session.scalar(
            select(Participant).order_by(Participant.id.desc()).limit(1)
        )
        assert participant.themes == []

    page = (await client.get("/")).text
    assert "Mes centres d'intérêt" not in page, "aucun thème : rien à filtrer"


async def test_more_than_three_themes_is_no_longer_refused_here(
    client, session_factory
) -> None:
    """Ce test disait le contraire avant le chantier profil : `/mes-themes` appelait
    alors `valider_suggestion`, plafonnée comme l'étiquetage d'un débat. Il est
    **relu** plutôt que supprimé, comme `test_the_labels_became_links_at_j6` — il dit
    maintenant ce que l'écran accepte réellement, `valider_interets` en tête."""
    from sqlalchemy import select

    reponse = await client.post(
        "/mes-themes",
        data={"themes": ["sante", "logement", "transports", "energie"], "retour": "/"},
        follow_redirects=False,
    )
    assert reponse.status_code == 303, reponse.text

    async with session_factory() as session:
        participant = await session.scalar(
            select(Participant).order_by(Participant.id.desc()).limit(1)
        )
        assert sorted(participant.themes) == [
            "energie", "logement", "sante", "transports",
        ]


@pytest.mark.parametrize(
    "retour",
    ["https://ailleurs.example/", "//ailleurs.example/", "javascript:alert(1)"],
)
async def test_a_return_path_never_leaves_the_site(client, retour: str) -> None:
    """Sans ce garde-fou, le site devient un tremplin : on clique un lien du
    Rond-Point, on enregistre, et on atterrit sur une page qui n'est pas la nôtre —
    ce qui sert surtout à rendre crédible une fausse page de connexion.

    `//ailleurs.example` est le cas qu'on oublie : il commence bien par une barre
    oblique, et le navigateur y lit une adresse absolue.
    """
    reponse = await client.post(
        "/mes-themes", data={"themes": ["sante"], "retour": retour},
        follow_redirects=False,
    )
    assert reponse.status_code == 303
    assert reponse.headers["location"] == "/"


async def test_registration_offers_the_step_only_to_those_who_never_answered(
    client_factory,
) -> None:
    """L'étape est APRÈS l'inscription, passable d'un lien, et elle ne s'affiche pas
    pour qui a déjà réglé ses thèmes en anonyme — c'est la même ligne `participant`,
    elle porte déjà la réponse."""
    from tests.conftest import PASSWORD

    async with client_factory() as neuf:
        reponse = await neuf.post(
            "/compte/inscription",
            data={"username": "sansreponse@exemple.fr", "password": PASSWORD, "suivant": "/"},
            follow_redirects=False,
        )
        assert reponse.headers["location"] == "/mes-themes?retour=%2F"

    async with client_factory() as deja:
        await deja.post("/mes-themes", data={"themes": ["sante"], "retour": "/"},
                        follow_redirects=False)
        reponse = await deja.post(
            "/compte/inscription",
            data={"username": "dejarepondu@exemple.fr", "password": PASSWORD, "suivant": "/"},
            follow_redirects=False,
        )
        assert reponse.headers["location"] == "/"


async def test_the_account_page_shows_the_themes_and_links_to_the_one_screen(
    client,
) -> None:
    """`/compte` ne recopie pas la grille : deux formulaires pour une même préférence
    auraient fini par ne plus dire la même chose."""
    from tests.conftest import PASSWORD

    await client.post("/mes-themes", data={"themes": ["transports"], "retour": "/"},
                      follow_redirects=False)
    # L'inscription par le FORMULAIRE : c'est elle qui pose le cookie de session que
    # `/compte` exige. L'inscription par l'API ne connecte pas la page HTML.
    inscription = await client.post(
        "/compte/inscription",
        data={"username": "compte-themes@exemple.fr", "password": PASSWORD, "suivant": "/"},
        follow_redirects=False,
    )
    assert inscription.status_code == 303

    page = (await client.get("/compte")).text
    assert "Transports et mobilités" in page
    assert "/mes-themes?retour=%2Fcompte" in page
