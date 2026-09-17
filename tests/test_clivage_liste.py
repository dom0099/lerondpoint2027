"""L4 et L5 — les mesures du chantier L dans les listes : les tris, l'étiquette, le compte.

Le pendant « écran » des deux fichiers précédents du chantier : `test_clivage.py`
éprouve la définition sans base, `test_clivage_passage.py` l'écrit sur un vrai passage
d'analyse, et celui-ci vérifie ce que le visiteur en voit sur l'accueil et sur la page
« Débats ».

Les deux onglets de mesure — « Le plus clivant » (L4) et « Le plus consensuel » (L5) —
sont éprouvés ici ensemble : ils lisent la même sous-requête, apparaissent ensemble et
traitent les débats non mesurés de la même façon. Les séparer aurait dupliqué chaque
test pour une seule différence, celle de la colonne lue.

**Deux manières de fabriquer un débat mesuré cohabitent ici, et c'est délibéré.** Les
tests d'ORDRE posent les calculs à la main : ce qui s'y éprouve est une clause `ORDER
BY`, et faire tourner red-dwarf trois fois pour obtenir trois scores distincts coûterait
des secondes sans rien prouver de plus. Les tests d'ÉTIQUETTE, eux, passent par le vrai
passage d'analyse — c'est là que le compte affiché doit être celui que la chaîne écrit,
et un `n_clivantes` posé à la main ne le prouverait pas.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.analysis import pipeline
from app.models import (
    AnalysisRun,
    AnalysisStatus,
    Conversation,
    ConversationState,
    ModerationStatus,
    Statement,
    Vote,
)
from app.services import accueil as accueil_service
from tests.conftest import open_conversation
from tests.test_analysis import _populate


async def _calcul(
    session, conversation_id, *, clivage, n_clivantes, consensus=None, quand=None
):
    """Un calcul abouti posé à la main, avec le score qu'on veut lui donner.

    `finished_at` est explicite : c'est lui qui désigne le « dernier calcul », et deux
    calculs créés dans la même seconde de test ne se départageraient pas autrement.
    """
    run = AnalysisRun(
        conversation_id=conversation_id,
        status=AnalysisStatus.ok,
        finished_at=quand or datetime.now(timezone.utc),
        clivage=clivage,
        consensus=consensus,
        n_clivantes=n_clivantes,
    )
    session.add(run)
    await session.commit()
    return run


async def _trois_debats(moderator_client, session_factory):
    """Trois débats : deux mesurés, inégalement, et un jamais calculé.

    Le plus ancien est le plus clivant — sans quoi le tri par clivage et le tri par
    date rendraient le même ordre, et le test passerait sans rien vérifier.

    **Les deux scores ne sont pas en miroir**, et c'est le point du peuplement : le
    débat le plus clivant est aussi le plus consensuel. C'est parfaitement possible —
    les deux moyennes regardent les deux bouts d'un même classement, et un débat de
    cinq fractures nettes et cinq accords solides les obtient toutes les deux. Un
    peuplement où le consensus serait `1 − clivage` laisserait passer une implémentation
    qui trierait à l'envers la seconde liste.
    """
    _, tiede, _ = await open_conversation(
        moderator_client, statements=2, title="Débat tiède mais mesuré"
    )
    _, brulant, _ = await open_conversation(
        moderator_client, statements=2, title="Débat qui divise pour de bon"
    )
    _, muet, _ = await open_conversation(
        moderator_client, statements=2, title="Débat jamais calculé"
    )
    async with session_factory() as session:
        ids = {
            slug: identifiant
            for identifiant, slug in (
                await session.execute(select(Conversation.id, Conversation.slug))
            ).all()
        }
        await _calcul(
            session, ids[brulant], clivage=0.61, consensus=0.66, n_clivantes=3
        )
        await _calcul(
            session, ids[tiede], clivage=0.42, consensus=0.48, n_clivantes=0
        )
    return tiede, brulant, muet


# --- le tri « Le plus clivant » -----------------------------------------------------


async def test_the_divisive_sort_ranks_by_score_and_leaves_the_unmeasured_last(
    moderator_client, client, session_factory
) -> None:
    """Le classement descend par clivage, et les débats sans score ferment la marche.

    Le piège que ce test garde est le même qu'au tri par votes, retourné : PostgreSQL
    place les NULL EN TÊTE d'un ordre décroissant. Sans `nullslast`, « Le plus clivant »
    commencerait donc par les débats que personne n'a mesurés — et un `coalesce(…, 0)`
    à la place, comme pour les votes, aurait déclaré ces débats consensuels, ce que le
    L1 interdit : « pas encore mesuré » n'est pas « pas clivant ».
    """
    await _trois_debats(moderator_client, session_factory)

    page = (await client.get("/?tri=clivant")).text
    assert page.index("Débat qui divise pour de bon") < page.index(
        "Débat tiède mais mesuré"
    )
    assert page.index("Débat tiède mais mesuré") < page.index("Débat jamais calculé")

    # Et l'écran le dit, plutôt que de laisser lire la fin de la liste comme
    # « les moins clivants ».
    assert "ne sont pas encore calculés figurent à la suite" in page


async def test_the_default_order_is_never_the_divisive_one(
    moderator_client, client, session_factory
) -> None:
    """L'accueil reste rangé par date, et c'est une position éditoriale.

    Mettre les débats les plus divisés en tête d'un site dont la mission affichée est
    de dégager des accords dirait le contraire de ce que la page promet.
    """
    await _trois_debats(moderator_client, session_factory)

    assert accueil_service.TRI_DEFAUT == "recent"
    defaut = (await client.get("/")).text
    assert defaut.index("Débat jamais calculé") < defaut.index(
        "Débat qui divise pour de bon"
    )


async def test_the_tab_appears_only_once_a_debate_is_measured(
    moderator_client, client, session_factory
) -> None:
    """Un onglet qui rendrait la même liste que « Récent » n'apprend rien.

    Même raisonnement qu'au J6 pour le troisième onglet, absent tant que le visiteur
    n'a pas réglé ses thèmes.
    """
    _, slug, _ = await open_conversation(moderator_client, statements=2)

    avant = (await client.get("/")).text
    assert "Le plus clivant" not in avant
    assert "Le plus consensuel" not in avant
    assert "Le plus voté" in avant, "les deux autres onglets ne bougent pas"

    async with session_factory() as session:
        conversation = (await session.scalars(select(Conversation))).one()
        await _calcul(session, conversation.id, clivage=0.55, n_clivantes=1)

    apres = (await client.get("/")).text
    assert "Le plus clivant" in apres
    assert "Le plus consensuel" in apres
    assert "tri=clivant" in apres and "tri=consensuel" in apres

    # Sur la page « Débats » aussi : les deux écrans rendent la même barre.
    debats = (await client.get("/debats")).text
    assert "Le plus clivant" in debats and "Le plus consensuel" in debats


async def test_asking_for_a_sort_that_is_not_offered_shows_the_page(
    moderator_client, client
) -> None:
    """`?tri=clivant` avant toute mesure retombe sur « Récent », et ne lève pas.

    C'est la règle de `tri_valide` depuis le G2 : une adresse recopiée à la main n'est
    pas une attaque, et un 422 en réponse ferait perdre la liste à qui voulait la lire.
    Ce cas-ci n'est pas une faute de frappe mais un lien partagé qui a vieilli — une
    liste vidée de ses scores par la purge le produit tout seul.
    """
    await open_conversation(moderator_client, statements=2)
    adresses = (
        "/?tri=clivant",
        "/debats?tri=clivant",
        "/debats/suite?tri=clivant",
        "/?tri=consensuel",
        "/debats?tri=consensuel",
        "/debats/suite?tri=consensuel",
    )
    for adresse in adresses:
        reponse = await client.get(adresse)
        assert reponse.status_code == 200, (adresse, reponse.status_code)


async def test_the_divisive_sort_keeps_the_filter_and_the_filter_keeps_it(
    moderator_client, client, session_factory
) -> None:
    """Règle du J6, re-vérifiée sur le tri nouveau : tri et filtre sont orthogonaux.

    Un clic sur « Le plus clivant » ne doit pas effacer silencieusement le thème
    demandé, et un clic sur un thème ne doit pas défaire le classement choisi.
    """
    _, dedans, _ = await open_conversation(
        moderator_client,
        statements=2,
        title="Débat de transports",
        themes=("transports",),
    )
    _, dehors, _ = await open_conversation(
        moderator_client,
        statements=2,
        title="Débat de santé",
        themes=("sante",),
    )
    async with session_factory() as session:
        for conversation in await session.scalars(select(Conversation)):
            await _calcul(session, conversation.id, clivage=0.5, n_clivantes=1)

    page = (await client.get("/?tri=clivant&theme=transports")).text
    assert "Débat de transports" in page
    assert "Débat de santé" not in page
    # L'onglet du tri emporte le filtre courant…
    assert (
        "tri=votes&amp;theme=transports" in page or "tri=votes&theme=transports" in page
    )
    # …et depuis un autre tri, l'onglet du clivage emporte le filtre lui aussi.
    autre = (await client.get("/?tri=recent&theme=transports")).text
    assert (
        "tri=clivant&amp;theme=transports" in autre
        or "tri=clivant&theme=transports" in autre
    )


async def test_the_batches_are_ordered_like_the_page_that_asked_for_them(
    moderator_client, client, session_factory
) -> None:
    """La fournée suivante suit le MÊME tri, sinon la onzième ligne rompt l'ordre.

    Le défilement demande la suite avec le tri courant ; s'il retombait sur le défaut,
    une liste classée par clivage se poursuivrait par date sans que rien ne le dise.
    """
    tiede, brulant, _ = await _trois_debats(moderator_client, session_factory)

    fournee = (await client.get("/debats/suite?tri=clivant&decalage=0")).json()
    assert fournee["nombre"] == 3
    html = fournee["html"]
    assert html.index("Débat qui divise pour de bon") < html.index(
        "Débat tiède mais mesuré"
    )


# --- l'onglet « Le plus consensuel » : des propositions, pas des débats (L6) -------


async def test_the_consensual_tab_lists_statements_and_not_debates(
    moderator_client, client, session_factory
) -> None:
    """Le quatrième onglet change l'OBJET de la liste, il ne la réordonne pas.

    Les trois autres rangent les mêmes cartes de débats autrement ; celui-ci montre
    des propositions. Le test le vérifie par ce qui disparaît autant que par ce qui
    paraît : plus de carte, plus de mini-barre.
    """
    await _debat_mesure(
        moderator_client, session_factory, passages=1, titre="Débat mesuré"
    )

    page = (await client.get("/?tri=consensuel")).text
    assert "carte-debat" not in page, "l'onglet ne montre plus de cartes de débats"
    assert "accords" in page
    assert "tous les groupes d'opinion" in page.lower(), "le chapeau dit ce qu'on lit"


async def test_only_statements_every_group_leans_the_same_way_are_listed(
    moderator_client, client, session_factory
) -> None:
    """La règle (b) du L3 gouverne cette liste aussi, et c'est la même fonction.

    Le débat de `_debat_mesure` oppose deux camps parfaits sur CHACUNE de ses huit
    propositions : aucune n'est approuvée — ni rejetée — par les deux groupes à la
    fois. La liste doit donc rester vide, et le dire.

    Si elle se remplissait, le site rangerait sous « ce sur quoi les groupes se
    rejoignent » des propositions où un groupe est à 0 % — exactement le défaut que le
    client a fait corriger le 6 septembre 2026 sur l'écran de fin de parcours.
    """
    await _debat_mesure(
        moderator_client, session_factory, passages=1, titre="Débat sans accord"
    )

    page = (await client.get("/?tri=consensuel")).text
    assert "Aucun accord relevé" in page
    assert "cette liste se" in page, "un état vide n'est pas une panne"


async def test_a_statement_everyone_approves_reaches_the_list_with_its_debate(
    moderator_client, client, session_factory
) -> None:
    """Une proposition que les deux camps approuvent paraît, avec sa direction.

    C'est le pendant exact du test précédent : le même débat, plus une proposition
    commune, et la liste cesse d'être vide.
    """
    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8, title="Débat avec un accord"
    )
    async with session_factory() as session:
        participants = await _populate(
            session, conversation_id, statements, n_participants=10
        )
        commune = Statement(
            conversation_id=conversation_id,
            text="Celle-ci, tout le monde la soutient.",
            moderation_status=ModerationStatus.approved,
        )
        session.add(commune)
        await session.flush()
        for participant in participants:
            session.add(
                Vote(participant_id=participant.id, statement_id=commune.id, value=1)
            )
        conversation = await session.get(Conversation, conversation_id)
        await session.commit()
        await pipeline.analyse(session, conversation)

    page = (await client.get("/?tri=consensuel")).text
    assert "Celle-ci, tout le monde la soutient." in page
    assert "Les groupes l'approuvent" in page
    # La ligne porte le débat d'où elle vient, et le lien mène aux barres qui la
    # vérifient : c'est ce qui autorise la phrase sans les imprimer ici.
    assert f'href="/c/{slug}"' in page
    assert "Débat avec un accord" in page


async def test_a_debate_places_at_most_three_statements(
    moderator_client, client, session_factory
) -> None:
    """Un seul débat ne remplit pas la vitrine.

    Règle éditoriale et non limite technique : sans elle, un débat mûr et très
    consensuel occuperait les dix lignes, et le site montrerait un débat au lieu de
    se montrer lui-même.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8, title="Débat très consensuel"
    )
    async with session_factory() as session:
        participants = await _populate(
            session, conversation_id, statements, n_participants=10
        )
        communes = []
        for index in range(5):
            commune = Statement(
                conversation_id=conversation_id,
                text=f"Accord unanime n°{index}.",
                moderation_status=ModerationStatus.approved,
            )
            session.add(commune)
            communes.append(commune)
        await session.flush()
        for participant in participants:
            for commune in communes:
                session.add(
                    Vote(
                        participant_id=participant.id,
                        statement_id=commune.id,
                        value=1,
                    )
                )
        conversation = await session.get(Conversation, conversation_id)
        await session.commit()
        await pipeline.analyse(session, conversation)
        accords = await accueil_service.accords_en_cours(session)

    assert len(accords) == accueil_service.ACCORDS_PAR_DEBAT
    assert all(a.slug_debat == accords[0].slug_debat for a in accords)


async def test_closed_consultations_are_not_ongoing(
    moderator_client, client, session_factory
) -> None:
    """« Les débats en cours » se prend au pied de la lettre.

    Ce que les groupes d'une consultation close ont fini par accepter n'appartient plus
    à l'actualité du site — même règle que le compteur de propositions ouvertes, qui
    ignore les consultations closes depuis le G2.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=8, title="Consultation close"
    )
    async with session_factory() as session:
        participants = await _populate(
            session, conversation_id, statements, n_participants=10
        )
        commune = Statement(
            conversation_id=conversation_id,
            text="Un accord qui ne compte plus.",
            moderation_status=ModerationStatus.approved,
        )
        session.add(commune)
        await session.flush()
        for participant in participants:
            session.add(
                Vote(participant_id=participant.id, statement_id=commune.id, value=1)
            )
        conversation = await session.get(Conversation, conversation_id)
        await session.commit()
        await pipeline.analyse(session, conversation)
        assert await accueil_service.accords_en_cours(session), "avant la clôture"
        conversation.state = ConversationState.closed
        await session.commit()
        assert not await accueil_service.accords_en_cours(session)


async def test_the_consensual_tab_keeps_the_theme_filter(
    moderator_client, client, session_factory
) -> None:
    """Règle du J6 sur le quatrième onglet : le filtre tient, et le tri le conserve."""
    for titre, theme in (
        ("Débat de transports", "transports"),
        ("Débat de santé", "sante"),
    ):
        conversation_id, _, statements = await open_conversation(
            moderator_client, statements=8, title=titre, themes=(theme,)
        )
        async with session_factory() as session:
            participants = await _populate(
                session,
                conversation_id,
                statements,
                n_participants=10,
                prefixe=theme,
            )
            commune = Statement(
                conversation_id=conversation_id,
                text=f"Accord de {theme}.",
                moderation_status=ModerationStatus.approved,
            )
            session.add(commune)
            await session.flush()
            for participant in participants:
                session.add(
                    Vote(
                        participant_id=participant.id,
                        statement_id=commune.id,
                        value=1,
                    )
                )
            conversation = await session.get(Conversation, conversation_id)
            await session.commit()
            await pipeline.analyse(session, conversation)

    page = (await client.get("/?tri=consensuel&theme=transports")).text
    assert "Accord de transports" in page
    assert "Accord de sante" not in page
    assert (
        "tri=consensuel&amp;theme=transports" in page
        or "tri=consensuel&theme=transports" in page
    )


async def test_the_consensual_tab_shows_no_score(
    moderator_client, client, session_factory
) -> None:
    """Des mots et des entiers, ici comme ailleurs : aucun consensus continu.

    Cet onglet n'affiche aucun chiffre de mesure — ni le consensus d'un débat, ni celui
    d'une proposition. Le seul nombre de la ligne est un compte de votes.
    """
    await _trois_debats(moderator_client, session_factory)
    page = (await client.get("/?tri=consensuel")).text
    for interdit in ("0.66", "0,66", "66 %", "0.48", "0,48"):
        assert interdit not in page, interdit


async def test_the_batches_of_the_consensual_tab_are_statements_too(
    moderator_client, client, session_factory
) -> None:
    """Le défilement de `/debats` sert des propositions sous cet onglet, pas des cartes.

    Le piège gardé : trois routes servaient la même paire `(tranche, il en reste)`, et
    laisser la fournée choisir seule aurait rendu des cartes de débats sous un onglet
    qui montre des propositions — un défaut qui n'apparaît qu'après avoir déroulé.
    """
    await _trois_debats(moderator_client, session_factory)
    fournee = (await client.get("/debats/suite?tri=consensuel&decalage=0")).json()
    assert "carte-debat" not in fournee["html"]


# --- l'étiquette et le compte, sur un vrai passage d'analyse ------------------------


async def _debat_mesure(moderator_client, session_factory, *, passages, titre):
    """Un débat de dix votants en DEUX CAMPS PARFAITS, analysé `passages` fois.

    `_populate` et non le peuplement de `test_resultats.py` : celui-ci fait dévier
    chaque votant sur une proposition qui lui est propre, ce qui suffit au clustering
    mais ramène le clivage à 0,44 — sous le seuil, donc aucune proposition comptée et
    rien à étiqueter. Deux camps parfaits à cinq votants chacun franchissent le seuil
    sur toutes les propositions, comme le L2 l'a mesuré.
    """
    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8, title=titre
    )
    async with session_factory() as session:
        await _populate(session, conversation_id, statements, n_participants=10)
        conversation = await session.get(Conversation, conversation_id)
        for _ in range(passages):
            await pipeline.analyse(session, conversation)
    return conversation_id, slug


async def test_the_label_waits_for_a_second_run(
    moderator_client, client, session_factory
) -> None:
    """Un seul calcul ne pose pas l'étiquette ; le second la pose.

    C'est la règle du K0 — « il faut deux échecs, pas un » — retournée du côté de
    l'affirmation. Le calcul tourne toutes les quinze minutes : une étiquette accrochée
    au dernier calcul seul clignoterait entre deux visites.
    """
    await _debat_mesure(
        moderator_client, session_factory, passages=1, titre="Débat en un passage"
    )

    # Sur la page « Débats » : l'accueil ne montre plus l'étiquette depuis le chantier
    # I2 (instruction 17), il ne peut donc plus témoigner de la règle des deux calculs.
    apres_un = (await client.get("/debats")).text
    assert "etiquette--clivant" not in apres_un
    # Le compte, lui, s'affiche dès le premier calcul : c'est un fait mesuré, pas une
    # étiquette qui qualifie le débat.
    assert "divisent les groupes" in apres_un

    async with session_factory() as session:
        conversation = (await session.scalars(select(Conversation))).one()
        await pipeline.analyse(session, conversation)

    apres_deux = (await client.get("/debats")).text
    assert "etiquette--clivant" in apres_deux
    assert ">Clivant<" in apres_deux

    # Et l'accueil, lui, ne la pose jamais — mais garde le compte, qui est un fait.
    accueil = (await client.get("/")).text
    assert "etiquette--clivant" not in accueil
    assert "divisent les groupes" in accueil


async def test_the_label_is_never_contradicted_by_the_count_beside_it(
    moderator_client, client, session_factory
) -> None:
    """La règle laissée par le L3, appliquée à la carte d'un débat.

    L'étiquette repose sur l'entier affiché et non sur le score continu du débat : le
    mot et le nombre ne peuvent donc pas se contredire. Ce test le vérifie là où ça
    compte — sur la page, pas dans le module.
    """
    conversation_id, _ = await _debat_mesure(
        moderator_client, session_factory, passages=2, titre="Débat étiqueté"
    )
    async with session_factory() as session:
        conversations = list(await session.scalars(select(Conversation)))
        part = (await accueil_service.repartitions(session, conversations))[
            conversation_id
        ]

    assert part.clivant is True
    assert part.n_clivantes is not None and part.n_clivantes >= 1, (
        "une étiquette posée sans qu'aucune proposition ne soit comptée serait "
        "démentie par la ligne imprimée juste en dessous"
    )
    page = (await client.get("/")).text
    assert f'<span class="chiffre">{part.n_clivantes}</span>' in page


async def test_a_debate_without_a_divisive_statement_says_nothing(
    moderator_client, client, session_factory
) -> None:
    """Ni étiquette, ni « 0 proposition » : le silence est la seule chose vraie.

    Un compte nul et un débat jamais mesuré donnent le même écran, et c'est voulu —
    annoncer « 0 » sur un débat qu'on vient de mesurer pour la première fois affirmerait
    plus que ce qu'on sait.
    """
    _, slug, _ = await open_conversation(
        moderator_client, statements=2, title="Débat sans fracture"
    )
    async with session_factory() as session:
        conversation = (await session.scalars(select(Conversation))).one()
        hier = datetime.now(timezone.utc) - timedelta(hours=1)
        await _calcul(session, conversation.id, clivage=0.4, n_clivantes=0, quand=hier)
        await _calcul(session, conversation.id, clivage=0.4, n_clivantes=0)

    page = (await client.get("/debats")).text
    assert "etiquette--clivant" not in page
    assert "divise les groupes" not in page
    assert "divisent les groupes" not in page


async def test_the_label_leaves_as_soon_as_one_run_stops_counting(
    moderator_client, client, session_factory
) -> None:
    """La pose demande deux calculs, le retrait un seul.

    L'asymétrie penche du côté prudent : celui qui n'affirme rien. Le piège gardé ici
    est un « au moins deux calculs clivants dans l'histoire » au lieu de « les deux
    derniers » — un débat clivant il y a six mois et calme depuis porterait alors
    l'étiquette pour toujours.
    """
    _, slug, _ = await open_conversation(
        moderator_client, statements=2, title="Débat apaisé"
    )
    async with session_factory() as session:
        conversation = (await session.scalars(select(Conversation))).one()
        base = datetime.now(timezone.utc) - timedelta(hours=3)
        for rang, compte in enumerate((4, 4, 0)):
            await _calcul(
                session,
                conversation.id,
                clivage=0.7,
                n_clivantes=compte,
                quand=base + timedelta(hours=rang),
            )

    page = (await client.get("/debats")).text
    assert "etiquette--clivant" not in page


async def test_no_continuous_score_ever_reaches_a_list(
    moderator_client, client, session_factory
) -> None:
    """Des mots et des entiers, jamais « 0,61 » ni « 61 % ».

    Règle du L0, et le L2 lui a donné sa justification chiffrée : rien de réel ne
    s'approche de 1 — le maximum observé sur le site est 0,627 —, si bien qu'un score
    présenté comme un pourcentage serait trompeur.
    """
    await _trois_debats(moderator_client, session_factory)

    for adresse in ("/?tri=clivant", "/debats?tri=clivant"):
        page = (await client.get(adresse)).text
        assert "0.61" not in page
        assert "0,61" not in page
        assert "61 %" not in page
