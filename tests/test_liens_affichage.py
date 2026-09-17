"""K3 — ce que le site DIT d'un lien mort, aux trois endroits où il le dit.

Aucun réseau ici non plus, et pour une raison différente des K1 et K2 : cette étape
n'interroge rien. Elle relit la table que le worker remplit. Les lignes sont donc
écrites à la main, avec les dates qu'il faut — c'est ce qui permet d'éprouver la règle
des deux constats à sept jours d'écart sans attendre une semaine.

Ce que ces tests gardent tient en une phrase : **on n'affiche que ce que `signale()`
autorise, et on n'efface jamais rien.**
"""

from datetime import datetime, timedelta, timezone

from app.models import Conversation, ConversationSource, LienVerifie, StatementSource, Verdict
from app.services.liens_verification import (
    ECHECS_POUR_SIGNALER,
    JOURS_POUR_SIGNALER,
    combien_a_corriger,
    empreinte_de,
    liens_a_corriger,
    signalements,
)

#: Une date fixe. Le signalement se juge sur les dates de la LIGNE (`premier_echec` et
#: `dernier_essai`), jamais sur l'heure qu'il est : un test qui dépendrait de
#: `now()` passerait aujourd'hui et échouerait le jour où la machine est lente.
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _lien(
    url="https://www.insee.fr/rapport",
    *,
    echecs=ECHECS_POUR_SIGNALER,
    jours=JOURS_POUR_SIGNALER,
    verdict=Verdict.injoignable,
    code=404,
) -> LienVerifie:
    """Une ligne de vérification, signalable par défaut.

    Les paramètres nomment exactement les trois conditions de `signale()` : le verdict,
    le nombre de constats, et l'écart entre le premier et le dernier. Un test qui veut
    éprouver un seuil n'en change qu'un.
    """
    return LienVerifie(
        empreinte=empreinte_de(url),
        url=url,
        domaine="insee.fr",
        verdict=verdict,
        code_http=code,
        echecs_consecutifs=echecs,
        premier_echec=T0 if verdict is Verdict.injoignable else None,
        dernier_essai=T0 + timedelta(days=jours),
        dernier_succes=None,
    )


async def _debat(moderator_client, session_factory, *, chiffres=(), apports=()):
    """Un débat publié, ses chiffres et les liens d'une de ses propositions.

    Les lignes sont ajoutées par leur clé étrangère, comme le fait l'application :
    affecter la collection déclencherait un chargement paresseux, qui lève
    `MissingGreenlet` en asynchrone.
    """
    from tests.conftest import open_conversation

    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=1, title="Débat aux liens morts"
    )
    async with session_factory() as session:
        for chiffre in chiffres:
            chiffre.conversation_id = conversation_id
            session.add(chiffre)
        for apport in apports:
            apport.statement_id = statements[0]
            session.add(apport)
        await session.commit()
    return conversation_id, slug, statements


def _chiffre(url, titre="Communes de moins de 500 habitants", valeur="3 200 communes"):
    return ConversationSource(position=1, titre=titre, valeur=valeur, url=url)


async def _poser(session_factory, *liens):
    async with session_factory() as session:
        for lien in liens:
            session.add(lien)
        await session.commit()


# --- le service : ce qui franchit le seuil, et ce qui ne le franchit pas ------------


async def test_a_reported_link_comes_back_with_the_date_it_went_missing(
    session_factory,
) -> None:
    """Et c'est `premier_echec` qui est rendu, pas `dernier_essai`.

    La distinction est ce que le lecteur lit : « injoignable depuis le 1er septembre »
    dit depuis quand la page manque ; la date du dernier essai dirait seulement quand
    on a regardé pour la dernière fois, ce qui n'intéresse personne.
    """
    await _poser(session_factory, _lien())
    async with session_factory() as session:
        dates = await signalements(session, ["https://www.insee.fr/rapport"])
    assert dates == {"https://www.insee.fr/rapport": T0}


async def test_a_single_failure_is_never_shown(session_factory) -> None:
    """Le seuil du K1 doit tenir jusqu'à l'écran, sans quoi il n'aura servi à rien."""
    await _poser(session_factory, _lien(echecs=1))
    async with session_factory() as session:
        assert await signalements(session, ["https://www.insee.fr/rapport"]) == {}


async def test_two_failures_too_close_together_are_never_shown(session_factory) -> None:
    await _poser(session_factory, _lien(jours=1))
    async with session_factory() as session:
        assert await signalements(session, ["https://www.insee.fr/rapport"]) == {}


async def test_an_inconclusive_verdict_is_kept_for_us_and_never_shown(
    session_factory,
) -> None:
    """403 et 5xx sont enregistrés et jamais montrés : le site refuse les robots ou va
    mal, il n'est pas mort. C'est une information pour nous, pas pour le lecteur."""
    await _poser(session_factory, _lien(verdict=Verdict.non_concluant, code=403))
    async with session_factory() as session:
        assert await signalements(session, ["https://www.insee.fr/rapport"]) == {}


async def test_the_same_page_written_two_ways_gets_the_same_verdict(
    session_factory,
) -> None:
    """Le doublon réel de la production, sous ses deux écritures possibles.

    Une seule ligne en base, et pourtant les DEUX adresses reçoivent leur date : c'est
    tout l'intérêt d'indexer par empreinte plutôt que par chaîne, et la raison pour
    laquelle `signalements()` rend un dictionnaire indexé par l'adresse reçue.
    """
    await _poser(session_factory, _lien("https://www.insee.fr/rapport"))
    async with session_factory() as session:
        dates = await signalements(
            session,
            ["https://www.insee.fr/rapport", "https://www.insee.fr/rapport#tableau-3"],
        )
    assert dates == {
        "https://www.insee.fr/rapport": T0,
        "https://www.insee.fr/rapport#tableau-3": T0,
    }


async def test_asking_about_nothing_asks_the_database_nothing(session_factory) -> None:
    async with session_factory() as session:
        assert await signalements(session, []) == {}
        assert await signalements(session, [None, ""]) == {}


# --- la page publique des chiffres -------------------------------------------------


async def test_the_public_page_says_the_link_is_reported_AND_keeps_it_clickable(
    client, moderator_client, session_factory
) -> None:
    """La décision du client, en un seul test : on informe, on ne retire pas.

    Le chiffre reste, le lien reste, le `href` reste. Le lecteur peut vouloir essayer
    quand même, ou chercher une copie archivée — et un faux positif qui aurait effacé
    la source aurait fait perdre en silence ce qui fondait un chiffre juste.
    """
    _, slug, _ = await _debat(
        moderator_client,
        session_factory,
        chiffres=[_chiffre("https://www.insee.fr/rapport")],
    )
    await _poser(session_factory, _lien())

    page = (await client.get(f"/c/{slug}/chiffres")).text
    assert "lien signalé injoignable depuis le 1 septembre 2026" in page
    assert 'href="https://www.insee.fr/rapport"' in page
    assert "3 200 communes" in page


async def test_the_public_page_says_nothing_below_the_threshold(
    client, moderator_client, session_factory
) -> None:
    """Un 404 vu une seule fois ne s'affiche pas — et la page ne change en RIEN."""
    _, slug, _ = await _debat(
        moderator_client,
        session_factory,
        chiffres=[_chiffre("https://www.insee.fr/rapport")],
    )
    await _poser(session_factory, _lien(echecs=1))

    page = (await client.get(f"/c/{slug}/chiffres")).text
    assert "injoignable" not in page
    assert 'href="https://www.insee.fr/rapport"' in page


async def test_a_link_that_is_not_displayed_is_not_reported_either(
    client, moderator_client, session_factory
) -> None:
    """Une adresse dont on ne sait pas nommer la destination n'est déjà pas montrée.

    Annoncer qu'elle est morte reviendrait à parler au lecteur d'un lien qu'il ne voit
    pas. La mention vit donc dans la branche du gabarit qui affiche le lien, et nulle
    part ailleurs.
    """
    _, slug, _ = await _debat(
        moderator_client,
        session_factory,
        chiffres=[_chiffre("javascript:alert(1)")],
    )
    await _poser(session_factory, _lien("javascript:alert(1)"))

    page = (await client.get(f"/c/{slug}/chiffres")).text
    assert "injoignable" not in page
    assert "Source non affichable" in page
    assert "javascript:alert(1)" not in page


# --- la page de vote, servie en JSON -----------------------------------------------


async def test_the_vote_page_is_told_which_link_is_dead(
    client, moderator_client, session_factory
) -> None:
    """La proposition servie porte la date, déjà écrite en français.

    Le script de la page de vote pose du `textContent` et ne met rien en forme : lui
    passer un `datetime` l'obligerait à formater une date en JavaScript, donc à écrire
    une seconde fois une règle d'affichage française.
    """
    _, slug, _ = await _debat(
        moderator_client,
        session_factory,
        apports=[StatementSource(position=1, url="https://www.insee.fr/rapport", label="Le bilan")],
    )
    await _poser(session_factory, _lien())

    detail = (await client.get(f"/api/conversations/{slug}")).json()
    sources = [s for st in detail["statements"] for s in st["sources"]]
    assert len(sources) == 1
    assert sources[0]["signale_le"] == "1 septembre 2026"
    assert sources[0]["url"] == "https://www.insee.fr/rapport"


async def test_a_live_link_carries_no_mention_at_all(
    client, moderator_client, session_factory
) -> None:
    """`None`, et non une chaîne vide : le gabarit et le script testent la présence."""
    _, slug, _ = await _debat(
        moderator_client,
        session_factory,
        apports=[StatementSource(position=1, url="https://www.insee.fr/rapport")],
    )
    await _poser(session_factory, _lien(verdict=Verdict.joignable, code=200, echecs=0))

    detail = (await client.get(f"/api/conversations/{slug}")).json()
    sources = [s for st in detail["statements"] for s in st["sources"]]
    assert sources[0]["signale_le"] is None


async def test_the_next_statement_endpoint_carries_the_mention_too(
    client, moderator_client, session_factory
) -> None:
    """C'est CE chemin que voit un votant : `/api/conversations/{slug}` sert la page
    entière, `next-statement` sert la proposition qu'il est en train de juger. Le
    second oublié, la mention n'existerait que sur une page que personne ne regarde."""
    _, slug, _ = await _debat(
        moderator_client,
        session_factory,
        apports=[StatementSource(position=1, url="https://www.insee.fr/rapport")],
    )
    await _poser(session_factory, _lien())

    suivante = (await client.get(f"/api/conversations/{slug}/next-statement")).json()
    assert suivante["statement"]["sources"][0]["signale_le"] == "1 septembre 2026"


# --- l'écran de modération ---------------------------------------------------------


async def test_moderation_lists_the_dead_link_and_the_way_back_to_it(
    moderator_client, session_factory
) -> None:
    """La table est indexée par adresse ; un modérateur corrige un chiffre dans un
    débat. Sans le chemin de retour, l'écran dirait « quelque chose est mort quelque
    part »."""
    conversation_id, _, _ = await _debat(
        moderator_client,
        session_factory,
        chiffres=[_chiffre("https://www.insee.fr/rapport", titre="Communes rurales")],
    )
    await _poser(session_factory, _lien())

    page = (await moderator_client.get("/moderation/liens")).text
    assert "https://www.insee.fr/rapport" in page
    assert "Communes rurales" in page
    assert "Débat aux liens morts" in page
    assert f"/moderation/conversations/{conversation_id}" in page


async def test_moderation_offers_no_button_to_check_a_link_on_demand(
    moderator_client, session_factory
) -> None:
    """La question 6, et sa réponse : non.

    Un bouton « vérifier maintenant » serait une requête sortante déclenchée à la
    demande vers une adresse fournie par un tiers — le cas d'école que le J0 a refusé.
    L'écran ne porte donc aucun formulaire, et c'est ce qui est gardé ici : une
    absence, qu'aucun autre test ne remarquerait.
    """
    await _debat(
        moderator_client,
        session_factory,
        chiffres=[_chiffre("https://www.insee.fr/rapport")],
    )
    await _poser(session_factory, _lien())

    page = (await moderator_client.get("/moderation/liens")).text
    assert "<form" not in page.split("<main>")[1]


async def test_the_same_page_cited_twice_makes_one_row_and_two_places_to_fix(
    moderator_client, session_factory
) -> None:
    """Le doublon réel de la production. Une ligne, deux corrections à faire."""
    await _debat(
        moderator_client,
        session_factory,
        chiffres=[
            _chiffre("https://www.insee.fr/rapport", titre="Premier chiffre"),
            ConversationSource(
                position=2,
                titre="Second chiffre",
                valeur="12,4 %",
                url="https://www.insee.fr/rapport",
            ),
        ],
    )
    await _poser(session_factory, _lien())

    async with session_factory() as session:
        signales = await liens_a_corriger(session)
    assert len(signales) == 1
    assert len(signales[0].citations) == 2
    assert {c.libelle for c in signales[0].citations} == {"Premier chiffre", "Second chiffre"}


async def test_a_link_nobody_publishes_any_more_leaves_the_list(
    moderator_client, session_factory
) -> None:
    """L'écran liste du travail à faire, pas un historique.

    Une adresse corrigée par un modérateur en disparaît à la correction, sans attendre
    un passage du worker — la ligne, elle, reste en base et cessera simplement d'être
    interrogée.
    """
    await _debat(moderator_client, session_factory, chiffres=[])
    await _poser(session_factory, _lien("https://www.insee.fr/plus-cite-nulle-part"))

    async with session_factory() as session:
        assert await liens_a_corriger(session) == []
    page = (await moderator_client.get("/moderation/liens")).text
    assert "Aucun lien signalé" in page


async def test_an_unapproved_statement_never_reaches_the_moderation_list(
    client, moderator_client, session_factory
) -> None:
    """Même règle qu'au recensement du K2 : ce qui n'est pas publié n'est pas affiché.

    Un lien porté par une proposition en attente n'est visible de personne ; le faire
    figurer dans une liste de corrections ferait travailler un modérateur sur un
    contenu qui sera peut-être rejeté.
    """
    from tests.conftest import depublier, open_conversation

    _, slug, _ = await open_conversation(moderator_client, statements=1, title="Débat en attente")
    depot = await client.post(
        f"/api/conversations/{slug}/statements",
        json={
            "text": "Une proposition qui attend son tour.",
            "sources": [{"url": "https://www.insee.fr/rapport", "label": "Le bilan"}],
        },
    )
    # Le dépôt publie, depuis le MOD-14 : l'état non publié se pose directement.
    await depublier(session_factory, depot.json()["id"])
    await _poser(session_factory, _lien())

    async with session_factory() as session:
        assert await liens_a_corriger(session) == []


async def test_the_moderation_bar_counts_exactly_what_the_screen_lists(
    moderator_client, session_factory
) -> None:
    """Un chiffre dans la barre qui ne correspondrait pas à la liste ouverte ferait
    chercher pour rien. Les deux passent donc par la même fonction."""
    await _debat(
        moderator_client,
        session_factory,
        chiffres=[_chiffre("https://www.insee.fr/rapport")],
    )
    await _poser(session_factory, _lien(), _lien("https://www.insee.fr/autre", echecs=1))

    async with session_factory() as session:
        assert await combien_a_corriger(session) == 1
    barre = (await moderator_client.get("/moderation/queue")).text
    assert "Liens signalés (1)" in barre


async def test_an_empty_screen_says_the_rule_rather_than_just_nothing(
    moderator_client,
) -> None:
    """Sur un écran qu'on ouvre deux fois par an, « aucun lien » sans explication
    laisse croire que rien n'est vérifié."""
    page = (await moderator_client.get("/moderation/liens")).text
    assert "Aucun lien signalé" in page
    assert str(JOURS_POUR_SIGNALER) in page
