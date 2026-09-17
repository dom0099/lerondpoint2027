"""Les liens attachés aux propositions, et les chiffres publics d'un débat.

Le J1 ne pose aucun écran : ce qui est éprouvé ici est la validation d'une adresse,
l'extraction du domaine affiché, et le fait que les tables tiennent leurs promesses —
en particulier le plafond de deux liens, qui n'est pas une règle du formulaire.
"""

import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    Conversation,
    ConversationSource,
    ModerationStatus,
    Statement,
    StatementSource,
)
from app.services.liens import (
    LIENS_MAX,
    LienInvalide,
    valider_lien,
    valider_liens,
)


# --- ce qui est refusé ------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "file:///etc/passwd",
        "exemple.fr/page",          # sans schéma
        "//exemple.fr/page",        # relative au protocole
        "/page",
    ],
)
def test_only_http_and_https_are_accepted(url: str) -> None:
    """Le champ est public et son contenu finit dans un attribut `href` affiché à
    tous. `javascript:` et `data:` y sont des vecteurs, pas des adresses."""
    with pytest.raises(LienInvalide):
        valider_lien(url)


def test_an_address_without_a_scheme_is_refused_and_not_guessed() -> None:
    """Préfixer « https:// » d'office reviendrait à décider à la place de l'auteur,
    sans qu'il voie ce qui a été décidé. Le refus est explicite."""
    with pytest.raises(LienInvalide) as erreur:
        valider_lien("www.insee.fr/statistiques")
    assert "http" in str(erreur.value)


def test_whitespace_inside_an_address_is_refused() -> None:
    with pytest.raises(LienInvalide):
        valider_lien("https://exemple.fr/a b")
    with pytest.raises(LienInvalide):
        valider_lien("https://exemple.fr/\nx")


def test_an_over_long_address_is_refused_before_anything_else() -> None:
    with pytest.raises(LienInvalide):
        valider_lien("https://exemple.fr/" + "a" * 2100)


def test_a_label_without_an_address_is_refused_rather_than_dropped() -> None:
    """Un libellé seul n'a rien à décrire : c'est un champ oublié. Le dire vaut mieux
    que de jeter la saisie sans un mot."""
    with pytest.raises(LienInvalide):
        valider_lien("", "Rapport de la Cour des comptes")
    assert valider_lien("", "") is None
    assert valider_lien(None) is None


# --- le domaine affiché, qui est le vrai sujet ------------------------------------


def test_the_displayed_domain_is_the_one_the_link_points_to() -> None:
    """Le test qui attrape l'affichage trompeur.

    Rien de ce qui s'affiche ne vient d'un tiers : le domaine est extrait de l'adresse
    par nous. Encore faut-il l'extraire correctement — un découpage naïf sur « / »
    puis « @ » montrerait ici « insee.fr » pour une adresse qui mène ailleurs.
    """
    assert valider_lien("https://www.insee.fr/fr/statistiques/1").domaine == "insee.fr"
    assert valider_lien("https://INSEE.FR/x").domaine == "insee.fr"
    assert valider_lien("https://exemple.fr:8443/x").domaine == "exemple.fr"
    # Le chemin ne fait pas le domaine.
    assert valider_lien("https://exemple.net/insee.fr/rapport").domaine == "exemple.net"
    # Ni le fragment, ni la requête.
    assert valider_lien("https://exemple.net/?u=insee.fr#insee.fr").domaine == "exemple.net"


def test_an_address_carrying_credentials_is_refused() -> None:
    """`https://insee.fr@exemple.net/` mène à exemple.net.

    Afficher le vrai domaine sous une adresse que personne ne lit ainsi resterait
    trompeur pour qui survole le lien : le refus est plus sûr que l'affichage.
    """
    with pytest.raises(LienInvalide):
        valider_lien("https://insee.fr@exemple.net/rapport")
    with pytest.raises(LienInvalide):
        valider_lien("https://utilisateur:secret@exemple.net/")


def test_a_homograph_domain_is_shown_in_punycode() -> None:
    """Deux domaines distincts peuvent s'écrire pareil à l'œil. Affiché en punycode,
    le domaine est laid mais vrai — c'est le bon arbitrage sur un lien qu'un lecteur
    va suivre."""
    lien = valider_lien("https://exemplé.fr/page")
    assert lien.domaine.startswith("xn--")
    assert "exemplé" not in lien.domaine


def test_the_stored_address_is_what_the_author_wrote() -> None:
    """Le domaine est recalculé à l'affichage, jamais stocké : une colonne `domaine`
    pourrait diverger de l'adresse après une correction, et le lecteur verrait alors
    un nom qui n'est plus celui vers lequel il va."""
    url = "https://www.insee.fr/fr/statistiques/1?x=2"
    assert valider_lien(url).url == url


# --- le plafond de deux ------------------------------------------------------------


def test_at_most_two_links_per_statement() -> None:
    assert LIENS_MAX == 2
    with pytest.raises(LienInvalide):
        valider_liens([
            ("https://a.fr", None), ("https://b.fr", None), ("https://c.fr", None),
        ])
    liens = valider_liens([("https://a.fr", "A"), ("https://b.fr", None)])
    assert [lien.domaine for lien in liens] == ["a.fr", "b.fr"]


def test_an_empty_slot_between_two_links_does_not_shift_anything() -> None:
    """Remplir le second champ sans le premier est fréquent et ne veut rien dire de
    particulier : les vides sont ignorés, pas comptés."""
    liens = valider_liens([("", None), ("https://b.fr", None)])
    assert [lien.url for lien in liens] == ["https://b.fr"]
    assert valider_liens([("", None), ("", "")]) == []


def test_the_same_address_twice_is_refused() -> None:
    """Deux lignes qui montreraient le même domaine sous la même proposition : c'est
    une faute de recopie, pas une source de plus."""
    with pytest.raises(LienInvalide):
        valider_liens([("https://a.fr/x", "Une source"), ("https://a.fr/x", "Une autre")])


# --- ce que la base tient elle-même -----------------------------------------------


async def test_the_database_refuses_a_third_link(session_factory) -> None:
    """Le plafond est une règle de la BASE, et pas seulement du formulaire.

    Le back-office, la console et un import futur ne passent pas par le formulaire, et
    devraient pourtant s'y plier. C'est ce que garde la contrainte `position in (1, 2)`.
    """
    async with session_factory() as session:
        conversation = Conversation(slug="plafond", title="Plafond")
        statement = Statement(text="Une proposition.", conversation=conversation)
        session.add(conversation)
        await session.flush()
        session.add(StatementSource(statement_id=statement.id, position=3, url="https://c.fr"))
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_the_database_refuses_two_links_at_the_same_position(session_factory) -> None:
    async with session_factory() as session:
        conversation = Conversation(slug="position", title="Position")
        statement = Statement(text="Une proposition.", conversation=conversation)
        session.add(conversation)
        await session.flush()
        session.add_all([
            StatementSource(statement_id=statement.id, position=1, url="https://a.fr"),
            StatementSource(statement_id=statement.id, position=1, url="https://b.fr"),
        ])
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_a_link_dies_with_the_statement_that_carries_it(session_factory) -> None:
    """Le lien est du contenu publié qui appartient à une proposition : il suit son
    sort, et n'a donc ni statut de modération propre ni survie au-delà d'elle."""
    from sqlalchemy import func, select

    async with session_factory() as session:
        conversation = Conversation(slug="mortel", title="Mortel")
        statement = Statement(text="Une proposition.", conversation=conversation)
        statement.sources = [StatementSource(position=1, url="https://a.fr")]
        session.add(conversation)
        await session.commit()
        statement_id = statement.id

    async with session_factory() as session:
        await session.delete(await session.get(Statement, statement_id))
        await session.commit()

    async with session_factory() as session:
        restants = await session.scalar(
            select(func.count(StatementSource.id)).where(
                StatementSource.statement_id == statement_id
            )
        )
        assert restants == 0


async def test_links_are_read_back_in_the_order_they_were_placed(session_factory) -> None:
    """L'ordre affiché est celui de `position` et non celui des identifiants : c'est
    ce qui permettra d'échanger deux liens sans recréer les lignes."""
    async with session_factory() as session:
        conversation = Conversation(slug="ordre-liens", title="Ordre")
        statement = Statement(text="Une proposition.", conversation=conversation)
        statement.sources = [
            StatementSource(position=2, url="https://second.fr"),
            StatementSource(position=1, url="https://premier.fr"),
        ]
        session.add(conversation)
        await session.commit()
        statement_id = statement.id

    async with session_factory() as session:
        statement = await session.get(Statement, statement_id)
        assert [s.url for s in statement.sources] == [
            "https://premier.fr", "https://second.fr",
        ]


async def test_a_public_figure_carries_two_dates_and_not_one(session_factory) -> None:
    """« Mis à jour le 5 septembre » ne dit pas si la donnée est de 2026 ou de 2019.

    Les deux dates sont indépendantes et facultatives séparément : un chiffre peut
    avoir été vérifié sans qu'on sache de quand il date, et l'inverse.
    """
    async with session_factory() as session:
        conversation = Conversation(slug="chiffres", title="Chiffres")
        conversation.sources = [
            ConversationSource(
                position=1,
                titre="Communes de moins de 500 habitants",
                valeur="3 200 communes",
                url="https://www.insee.fr/x",
                date_donnees=dt.date(2024, 1, 1),
                date_verification=dt.date(2026, 9, 5),
            ),
            ConversationSource(
                position=2, titre="Taux d'effort", valeur="12,4 %", url="https://insee.fr/y"
            ),
        ]
        session.add(conversation)
        await session.commit()
        conversation_id = conversation.id

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        await session.refresh(conversation, ["sources"])
        premier, second = conversation.sources
        assert premier.date_donnees == dt.date(2024, 1, 1)
        assert premier.date_verification == dt.date(2026, 9, 5)
        assert second.date_donnees is None and second.date_verification is None


async def test_a_public_figure_does_not_depend_on_any_statement(session_factory) -> None:
    """C'est ce qui justifie deux tables plutôt qu'une : le chiffre ne dépend d'aucune
    proposition et survit à toutes."""
    from sqlalchemy import func, select

    async with session_factory() as session:
        conversation = Conversation(slug="survie", title="Survie")
        statement = Statement(
            text="Une proposition.",
            conversation=conversation,
            moderation_status=ModerationStatus.approved,
        )
        conversation.sources = [
            ConversationSource(titre="Un chiffre", valeur="1", url="https://insee.fr/z")
        ]
        session.add(conversation)
        await session.commit()
        conversation_id, statement_id = conversation.id, statement.id

    async with session_factory() as session:
        await session.delete(await session.get(Statement, statement_id))
        await session.commit()

    async with session_factory() as session:
        restants = await session.scalar(
            select(func.count(ConversationSource.id)).where(
                ConversationSource.conversation_id == conversation_id
            )
        )
        assert restants == 1


# --- J2 : le lien de bout en bout, du formulaire à la page de vote -----------------


async def _conversation_ouverte(moderator_client):
    from tests.conftest import open_conversation

    return await open_conversation(moderator_client, statements=1, title="Débat sourcé")


async def test_a_participant_attaches_a_link_to_their_statement(
    client, moderator_client, session_factory
) -> None:
    from sqlalchemy import select

    _, slug, _ = await _conversation_ouverte(moderator_client)
    response = await client.post(
        f"/api/conversations/{slug}/statements",
        json={
            "text": "Le taux d'effort des locataires a augmenté.",
            "sources": [
                {"url": "https://www.insee.fr/fr/statistiques/1", "label": "Insee"},
                {"url": "https://www.ccomptes.fr/rapport", "label": None},
            ],
        },
    )
    assert response.status_code == 201, response.text

    async with session_factory() as session:
        statement = await session.scalar(
            select(Statement).where(Statement.id == response.json()["id"])
        )
        assert [(s.position, s.url, s.label) for s in statement.sources] == [
            (1, "https://www.insee.fr/fr/statistiques/1", "Insee"),
            (2, "https://www.ccomptes.fr/rapport", None),
        ]


async def test_a_refused_link_leaves_no_statement_behind(
    client, moderator_client, session_factory
) -> None:
    """Le contrôle a lieu AVANT toute écriture.

    Une proposition enregistrée dont le lien serait ensuite refusé serait publiée
    amputée de ce qui l'appuie, sans que son auteur le sache — il a vu une erreur, et
    croit que rien n'a été enregistré.
    """
    from sqlalchemy import func, select

    conversation_id, slug, _ = await _conversation_ouverte(moderator_client)
    avant = None
    async with session_factory() as session:
        avant = await session.scalar(
            select(func.count(Statement.id)).where(
                Statement.conversation_id == conversation_id
            )
        )

    response = await client.post(
        f"/api/conversations/{slug}/statements",
        json={
            "text": "Une proposition parfaitement valable.",
            "sources": [{"url": "javascript:alert(1)", "label": None}],
        },
    )
    assert response.status_code == 422
    assert "http" in response.json()["detail"]

    async with session_factory() as session:
        apres = await session.scalar(
            select(func.count(Statement.id)).where(
                Statement.conversation_id == conversation_id
            )
        )
    assert apres == avant


async def test_a_refused_link_does_not_consume_the_daily_quota(
    client, moderator_client
) -> None:
    """Même raison que pour un texte trop long : une adresse mal recopiée ne doit pas
    coûter une des dix propositions de la journée."""
    from app.services import rate_limit

    _, slug, _ = await _conversation_ouverte(moderator_client)
    for index in range(rate_limit.STATEMENTS_PER_PARTICIPANT + 2):
        refus = await client.post(
            f"/api/conversations/{slug}/statements",
            json={"text": f"Tentative n°{index}.", "sources": [{"url": "data:x"}]},
        )
        assert refus.status_code == 422

    accepte = await client.post(
        f"/api/conversations/{slug}/statements",
        json={"text": "Celle-ci est correcte.", "sources": []},
    )
    assert accepte.status_code == 201, accepte.text


async def test_more_than_two_links_is_refused_by_the_api(client, moderator_client) -> None:
    _, slug, _ = await _conversation_ouverte(moderator_client)
    response = await client.post(
        f"/api/conversations/{slug}/statements",
        json={
            "text": "Trois sources pour une phrase.",
            "sources": [{"url": "https://a.fr"}, {"url": "https://b.fr"}, {"url": "https://c.fr"}],
        },
    )
    assert response.status_code == 422


async def test_the_vote_page_is_served_the_domain_and_never_the_bare_address(
    client, moderator_client
) -> None:
    """Ce que le navigateur affiche vient du serveur.

    Le domaine n'est pas déduit de l'adresse côté navigateur : la règle « le nom montré
    est celui vers lequel le lien mène » est tenue à un seul endroit, ce qui est la
    seule façon qu'elle soit vraie partout.
    """
    conversation_id, slug, _ = await _conversation_ouverte(moderator_client)
    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}/statements",
        data={
            "text": "Une amorce sourcée.",
            "source_url": "https://www.insee.fr/fr/statistiques/2",
            "source_label": "Insee, 2024",
        },
        follow_redirects=False,
    )

    vues = []
    for _ in range(5):
        reponse = (await client.get(f"/api/conversations/{slug}/next-statement")).json()
        if reponse["statement"] is None:
            break
        vues.append(reponse["statement"])
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": reponse["statement"]["id"], "value": 0},
        )

    sourcees = [s for s in vues if s["sources"]]
    assert len(sourcees) == 1, [s["text"] for s in vues]
    (source,) = sourcees[0]["sources"]
    assert source["domaine"] == "insee.fr"
    assert source["label"] == "Insee, 2024"
    assert source["url"] == "https://www.insee.fr/fr/statistiques/2"


async def test_a_link_on_an_unapproved_statement_is_never_served(
    client, moderator_client, session_factory
) -> None:
    """Le lien suit exactement le sort de la proposition qui le porte : dès qu'elle
    n'est plus publiée — retirée, ou en attente — ni l'un ni l'autre n'est servi."""
    from sqlalchemy import select

    from tests.conftest import depublier

    _, slug, _ = await _conversation_ouverte(moderator_client)
    depot = await client.post(
        f"/api/conversations/{slug}/statements",
        json={"text": "Retirée depuis.", "sources": [{"url": "https://secret.fr/x"}]},
    )
    assert depot.status_code == 201
    await depublier(session_factory, depot.json()["id"])

    async with session_factory() as session:
        assert await session.scalar(
            select(StatementSource).where(StatementSource.url == "https://secret.fr/x")
        ) is not None, "le lien doit bien être stocké, seulement pas servi"

    for _ in range(6):
        reponse = (await client.get(f"/api/conversations/{slug}/next-statement")).json()
        if reponse["statement"] is None:
            break
        assert "secret.fr" not in str(reponse["statement"]["sources"])
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": reponse["statement"]["id"], "value": 0},
        )


async def test_a_seed_statement_can_carry_a_link(moderator_client, session_factory) -> None:
    """Question 3 : même champ, mêmes règles. C'est souvent là qu'un chiffre a le plus
    de valeur — l'amorce donne le ton et sera la première votée."""
    from sqlalchemy import select

    conversation_id, _, _ = await _conversation_ouverte(moderator_client)
    reponse = await moderator_client.post(
        f"/moderation/conversations/{conversation_id}/statements",
        data={
            "text": "Une amorce avec deux sources.",
            "source_url": ["https://insee.fr/a", "https://ccomptes.fr/b"],
            "source_label": ["Insee", ""],
        },
        follow_redirects=False,
    )
    assert reponse.status_code == 303, reponse.text

    async with session_factory() as session:
        statement = await session.scalar(
            select(Statement).where(Statement.text == "Une amorce avec deux sources.")
        )
        assert [(s.position, s.url, s.label) for s in statement.sources] == [
            (1, "https://insee.fr/a", "Insee"),
            (2, "https://ccomptes.fr/b", None),
        ]


async def test_a_proposed_conversation_keeps_each_seed_with_its_own_links(
    client, session_factory
) -> None:
    """L'appariement, qui est le vrai risque du formulaire `/proposer`.

    Trois propositions, dont seule la DEUXIÈME porte une source. Si les liens étaient
    relevés après filtrage des textes vides, ou comptés à part, la source glisserait
    sur une autre proposition — et le site publierait une affirmation appuyée par une
    source qui n'est pas la sienne.
    """
    from sqlalchemy import select

    reponse = await client.post(
        "/proposer",
        data={
            "title": "Trois amorces, une seule sourcée",
            "description": "",
            "statements": ["Première.", "Deuxième.", "Troisième."],
            "themes": ["institutions"],
            "source_url": ["", "", "https://insee.fr/deuxieme", "", "", ""],
            "source_label": ["", "", "La source de la deuxième", "", "", ""],
        },
        follow_redirects=False,
    )
    assert reponse.status_code == 303, reponse.text

    async with session_factory() as session:
        statements = list(
            await session.scalars(
                select(Statement)
                .where(Statement.text.in_(["Première.", "Deuxième.", "Troisième."]))
                .order_by(Statement.id)
            )
        )
        porteuses = {s.text: [src.url for src in s.sources] for s in statements}
    assert porteuses == {
        "Première.": [],
        "Deuxième.": ["https://insee.fr/deuxieme"],
        "Troisième.": [],
    }


async def test_a_bad_link_on_the_proposal_form_names_which_statement(client) -> None:
    """Sur un formulaire à dix propositions, « adresse invalide » sans dire laquelle
    oblige à toutes les relire."""
    reponse = await client.post(
        "/proposer",
        data={
            "title": "Une adresse fautive",
            "description": "",
            "statements": ["Première.", "Deuxième.", "Troisième."],
            "themes": ["institutions"],
            "source_url": ["", "", "", "", "pas-une-adresse", ""],
            "source_label": ["", "", "", "", "", ""],
        },
    )
    assert reponse.status_code == 200
    assert "Proposition 3" in reponse.text
    # Ce qui a été tapé est réaffiché : un refus ne doit pas faire retaper le reste.
    assert "pas-une-adresse" in reponse.text
    assert "Deuxième." in reponse.text


# --- audit adverse : ce qui arrive si une adresse hostile ATTEINT la base ----------
#
# Le formulaire la refuse, et c'est éprouvé plus haut. Ces tests supposent le
# contraire : une ligne écrite par la console, un import futur, ou une régression du
# validateur. La question n'est plus « peut-on l'écrire » mais « que fait-on d'elle si
# elle est là » — et la réponse doit être la même sur les quatre surfaces d'affichage.

HOSTILES = [
    "javascript:alert(document.domain)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
]
ETIQUETTE_HOSTILE = '<img src=x onerror="alert(1)">'


async def _proposition_empoisonnee(moderator_client, session_factory, url):
    from tests.conftest import open_conversation

    conversation_id, slug, ids = await open_conversation(
        moderator_client, statements=1, title="Adresse hostile"
    )
    async with session_factory() as session:
        session.add(
            StatementSource(
                statement_id=ids[0], position=1, url=url, label=ETIQUETTE_HOSTILE
            )
        )
        await session.commit()
    return conversation_id, slug


@pytest.mark.parametrize("url", HOSTILES)
async def test_a_hostile_address_in_the_database_is_never_served_to_the_vote_page(
    client, moderator_client, session_factory, url: str
) -> None:
    """La règle de `statement_read` : un lien dont on ne sait pas nommer la destination
    n'est pas affiché. Le script de la page pose `lien.href = source.url` — si une
    adresse en `javascript:` arrivait jusque-là, elle s'exécuterait au clic."""
    _, slug = await _proposition_empoisonnee(moderator_client, session_factory, url)

    servi = (await client.get(f"/api/conversations/{slug}/next-statement")).json()
    assert servi["statement"]["sources"] == []

    page = (await client.get(f"/c/{slug}")).text
    assert "javascript:" not in page
    assert "onerror" not in page


@pytest.mark.parametrize("url", HOSTILES)
async def test_a_hostile_address_is_shown_but_NOT_clickable_in_moderation(
    moderator_client, session_factory, url: str
) -> None:
    """Deux exigences qui semblent contraires, et ne le sont pas.

    Le modérateur DOIT voir l'adresse en entier — c'est la décision du J2, et sans elle
    il approuve à l'aveugle. Mais elle ne doit pas être un lien qu'il peut suivre, fût-ce
    par curiosité. Visible toujours ; cliquable seulement quand la destination a un nom.
    """
    conversation_id, _ = await _proposition_empoisonnee(
        moderator_client, session_factory, url
    )
    page = (
        await moderator_client.get(f"/moderation/conversations/{conversation_id}")
    ).text
    assert url in page, "le modérateur doit voir l'adresse"
    assert f'href="{url}"' not in page, "mais elle ne doit pas être cliquable"
    assert "non cliquable" in page


@pytest.mark.parametrize("url", HOSTILES)
async def test_a_hostile_figure_address_is_not_a_link_on_the_public_page(
    client, moderator_client, session_factory, url: str
) -> None:
    """Le trou trouvé à l'audit du 5 septembre 2026 : ce gabarit faisait confiance à la
    base et rendait `href="{{ c.url }}"` sans condition. C'est la seule des quatre
    surfaces qui était PUBLIQUE."""
    from tests.conftest import open_conversation

    conversation_id, slug, _ = await open_conversation(
        moderator_client, statements=1, title="Chiffre hostile"
    )
    async with session_factory() as session:
        session.add(
            ConversationSource(
                conversation_id=conversation_id,
                titre="Un chiffre",
                valeur="1",
                url=url,
            )
        )
        await session.commit()

    page = (await client.get(f"/c/{slug}/chiffres")).text
    assert f'href="{url}"' not in page
    assert "javascript:" not in page
    assert "Source non affichable" in page


async def test_a_hostile_label_is_escaped_everywhere_it_is_shown(
    client, moderator_client, session_factory
) -> None:
    """Le libellé est écrit par un participant. Il est rendu à trois endroits : par
    Jinja en modération, et par `textContent` dans le script de la page de vote."""
    conversation_id, slug = await _proposition_empoisonnee(
        moderator_client, session_factory, "https://exemple.fr/page"
    )
    editeur = (
        await moderator_client.get(f"/moderation/conversations/{conversation_id}")
    ).text
    # Ce qui compte n'est pas l'absence du mot « onerror » — il survit comme TEXTE —
    # mais l'absence de la BALISE : sans `<`, aucun élément n'est créé, donc aucun
    # gestionnaire d'événement n'existe. Chercher le mot aurait été un test qui rassure
    # sans rien prouver.
    assert "<img" not in editeur, "aucune balise ne doit être créée"
    assert "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;" in editeur, (
        "le libellé doit être rendu en texte inerte, entièrement échappé"
    )

    # Servi à la page de vote, le libellé voyage en JSON et sera posé en textContent :
    # il n'entre jamais dans du HTML assemblé par concaténation.
    servi = (await client.get(f"/api/conversations/{slug}/next-statement")).json()
    assert servi["statement"]["sources"][0]["label"] == ETIQUETTE_HOSTILE


async def test_an_address_with_a_quote_cannot_break_out_of_its_attribute(
    client, moderator_client, session_factory
) -> None:
    """Une adresse contenant un guillemet sortirait de l'attribut `href` si Jinja
    n'échappait pas. Elle n'atteint pas la base par le formulaire — l'espace la fait
    refuser — mais la garde de rendu se vérifie tout de même."""
    conversation_id, slug, _ = await open_conversation_locale(
        moderator_client, "Guillemet"
    )
    async with session_factory() as session:
        session.add(
            ConversationSource(
                conversation_id=conversation_id,
                titre="Un chiffre",
                valeur="1",
                url='https://ok.fr/x" onmouseover="alert(1)',
            )
        )
        await session.commit()

    page = (await client.get(f"/c/{slug}/chiffres")).text
    assert 'onmouseover="alert(1)"' not in page
    assert "&#34;" in page or "&quot;" in page


async def open_conversation_locale(moderator_client, titre):
    from tests.conftest import open_conversation

    return await open_conversation(moderator_client, statements=1, title=titre)
