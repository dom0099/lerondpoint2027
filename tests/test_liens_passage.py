"""K2 — le recensement, la résolution sûre, et la passe du worker.

**Toujours aucun réseau.** Le client HTTP et le résolveur DNS sont tous deux passés en
argument ; les tests fournissent des doublures. Ce qui est éprouvé ici n'est pas
« est-ce que ça marche contre insee.fr » — cela dépendrait d'insee.fr — mais « est-ce
qu'on ne part que vers ce qu'on a le droit d'interroger, et qu'on n'y va qu'une fois ».
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import Conversation, ConversationSource, LienVerifie, StatementSource, Verdict
from app.services.liens_verification import (
    LIENS_PAR_PASSAGE,
    a_revoir,
    empreinte_de,
    hote_sur,
    ip_publique,
    recenser,
    verifier_les_liens,
)


class ClientFactice:
    """Rend le même code pour toutes les adresses, et retient ce qu'on lui a demandé."""

    def __init__(self, code=200):
        self.code, self.appels = code, []

    async def head(self, url):
        self.appels.append(url)
        return type("R", (), {"status_code": self.code})()


async def resolveur_public(hote):
    return ["93.184.216.34"]


async def resolveur_interne(hote):
    return ["127.0.0.1"]


async def resolveur_muet(hote):
    return []


# --- ce qui est recensé, et ce qui ne l'est pas ------------------------------------


async def test_only_what_the_site_actually_publishes_is_listed(
    client, moderator_client, session_factory
) -> None:
    """Un lien porté par une proposition non approuvée n'est visible de personne.

    Aller l'interroger reviendrait à faire une requête sortante au nom d'un contenu que
    le site n'a pas publié — et à prévenir un site tiers qu'on s'apprête peut-être à le
    citer.
    """
    from tests.conftest import open_conversation

    conversation_id, slug, ids = await open_conversation(
        moderator_client, statements=1, title="Recensement"
    )
    # Un chiffre : publié par construction.
    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}/chiffres",
        data={"titre": "Un chiffre", "valeur": "1", "url": "https://insee.fr/publie"},
        follow_redirects=False,
    )
    # Une proposition en attente de modération, avec son lien.
    depot = await client.post(
        f"/api/conversations/{slug}/statements",
        json={"text": "En attente.", "sources": [{"url": "https://insee.fr/en-attente"}]},
    )
    # Le dépôt publie, depuis le MOD-14 : l'état non publié se pose directement.
    from tests.conftest import depublier

    await depublier(session_factory, depot.json()["id"])

    async with session_factory() as session:
        assert await recenser(session) == 1
        urls = set(await session.scalars(select(LienVerifie.url)))
    assert urls == {"https://insee.fr/publie"}


async def test_listing_twice_adds_nothing_and_keeps_the_history(
    moderator_client, session_factory
) -> None:
    """Idempotent, et surtout : une adresse déjà connue n'est pas touchée — surtout pas
    son historique de vérification."""
    from tests.conftest import open_conversation

    conversation_id, _, _ = await open_conversation(
        moderator_client, statements=1, title="Deux fois"
    )
    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}/chiffres",
        data={"titre": "Un chiffre", "valeur": "1", "url": "https://insee.fr/a"},
        follow_redirects=False,
    )
    async with session_factory() as session:
        assert await recenser(session) == 1
        lien = await session.scalar(select(LienVerifie))
        lien.echecs_consecutifs = 3
        await session.commit()

        assert await recenser(session) == 0
        lien = await session.scalar(select(LienVerifie))
        assert lien.echecs_consecutifs == 3


async def test_the_same_page_cited_twice_is_listed_once(
    moderator_client, session_factory
) -> None:
    """Le doublon réel de la production : deux chiffres du débat sur le permis citent la
    même page. Une seule ligne, donc une seule requête."""
    from tests.conftest import open_conversation

    conversation_id, _, _ = await open_conversation(
        moderator_client, statements=1, title="Doublon"
    )
    for titre, url in [
        ("Premier", "https://insee.fr/rapport"),
        ("Second", "https://insee.fr/rapport#annexe"),
    ]:
        await moderator_client.post(
            f"/moderation/conversations/{conversation_id}/chiffres",
            data={"titre": titre, "valeur": "1", "url": url},
            follow_redirects=False,
        )
    async with session_factory() as session:
        assert await recenser(session) == 1


# --- l'ordre dans lequel on les reprend --------------------------------------------


async def test_a_never_checked_address_comes_first(session_factory) -> None:
    """`nulls_first` n'est pas une coquetterie de tri.

    Sans lui, PostgreSQL place les NULL en dernier en ordre croissant : une adresse
    jamais vérifiée attendrait que toutes les autres aient vieilli de sept jours avant
    d'être regardée une première fois.
    """
    vieux = datetime.now(timezone.utc) - timedelta(days=30)
    async with session_factory() as session:
        session.add_all([
            LienVerifie(empreinte=empreinte_de("https://insee.fr/vieux"),
                        url="https://insee.fr/vieux", domaine="insee.fr",
                        dernier_essai=vieux),
            LienVerifie(empreinte=empreinte_de("https://insee.fr/neuf"),
                        url="https://insee.fr/neuf", domaine="insee.fr"),
        ])
        await session.commit()
        ordre = [lien.url for lien in await a_revoir(session)]
    assert ordre[0] == "https://insee.fr/neuf"


async def test_a_recently_seen_address_is_left_alone(session_factory) -> None:
    async with session_factory() as session:
        session.add(
            LienVerifie(empreinte=empreinte_de("https://insee.fr/hier"),
                        url="https://insee.fr/hier", domaine="insee.fr",
                        dernier_essai=datetime.now(timezone.utc) - timedelta(days=1))
        )
        await session.commit()
        assert await a_revoir(session) == []


async def test_a_pass_is_bounded_by_its_count_not_by_the_catalogue(
    session_factory,
) -> None:
    """C'est ce nombre qui borne la charge : un catalogue qui grandit allonge le tour,
    il n'alourdit jamais un passage."""
    async with session_factory() as session:
        for index in range(LIENS_PAR_PASSAGE + 7):
            url = f"https://insee.fr/page-{index}"
            session.add(LienVerifie(empreinte=empreinte_de(url), url=url,
                                    domaine="insee.fr"))
        await session.commit()
        assert len(await a_revoir(session)) == LIENS_PAR_PASSAGE


# --- la résolution ------------------------------------------------------------------


@pytest.mark.parametrize(
    "adresse",
    ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254", "::1",
     "fd00::1", "0.0.0.0", "224.0.0.1", "239.255.255.250", "ff02::1"],
)
def test_a_private_address_is_never_public(adresse: str) -> None:
    """`169.254.169.254` est la plus importante de la liste : c'est d'elle que sortent
    les métadonnées d'instance sur la plupart des hébergeurs.

    Les trois dernières sont du **multicast**, et elles ont un statut à part : en
    Python 3.12, `224.0.0.1` a `is_global` à **vrai**. C'est cohérent pour l'IANA — la
    plage est routable — et faux pour nous, puisqu'une requête vers un groupe multicast
    part sur le réseau local. Ce test a attrapé la confiance excessive placée dans
    `is_global`, et le commentaire qui l'affirmait à tort.
    """
    assert ip_publique(adresse) is False


@pytest.mark.parametrize("adresse", ["93.184.216.34", "1.1.1.1", "2606:4700::1111"])
def test_a_routable_address_is_public(adresse: str) -> None:
    assert ip_publique(adresse) is True


async def test_a_host_that_resolves_anywhere_internal_is_refused() -> None:
    assert await hote_sur("insee.fr", resolveur_interne) is False


async def test_a_host_that_resolves_nowhere_is_refused() -> None:
    """Liste vide : on ne sait pas où l'on irait, donc on n'y va pas."""
    assert await hote_sur("insee.fr", resolveur_muet) is False


async def test_ALL_the_addresses_must_be_public_not_just_one() -> None:
    """Un nom qui rend une adresse publique et une adresse interne ferait tomber la
    requête sur l'une ou l'autre selon l'humeur de la pile réseau — et il suffirait de
    recommencer pour atteindre l'interne."""

    async def melange(hote):
        return ["93.184.216.34", "127.0.0.1"]

    assert await hote_sur("insee.fr", melange) is False


# --- la passe entière ----------------------------------------------------------------


async def _lien(session, url, domaine):
    session.add(LienVerifie(empreinte=empreinte_de(url), url=url, domaine=domaine))
    await session.commit()


async def test_the_pass_records_a_verdict_for_each_address(session_factory) -> None:
    async with session_factory() as session:
        await _lien(session, "https://insee.fr/a", "insee.fr")
        client = ClientFactice(code=404)
        comptes = await verifier_les_liens(
            session, client, pause=0, resolveur=resolveur_public
        )
        assert comptes == {"injoignable": 1}
        lien = await session.scalar(select(LienVerifie))
        assert lien.verdict is Verdict.injoignable
        assert lien.code_http == 404
        assert lien.premier_echec is not None


async def test_an_address_outside_the_whitelist_is_never_requested(
    session_factory,
) -> None:
    async with session_factory() as session:
        await _lien(session, "https://lemonde.fr/article", "lemonde.fr")
        client = ClientFactice(code=200)
        comptes = await verifier_les_liens(
            session, client, pause=0, resolveur=resolveur_public
        )
    assert comptes == {"hors_perimetre": 1}
    assert client.appels == [], "aucune requête ne doit partir"


async def test_an_address_resolving_internally_is_never_requested(
    session_factory,
) -> None:
    """Le garde-fou qui compte : la liste blanche dit d'où l'on part, la résolution dit
    que ce domaine ne mène pas chez nous. Un domaine autorisé dont le DNS pointerait un
    jour vers une adresse interne ne doit pas être contacté."""
    async with session_factory() as session:
        await _lien(session, "https://insee.fr/a", "insee.fr")
        client = ClientFactice(code=200)
        comptes = await verifier_les_liens(
            session, client, pause=0, resolveur=resolveur_interne
        )
    assert comptes == {"non_concluant": 1}
    assert client.appels == [], "aucune requête ne doit partir"


async def test_an_internal_resolution_is_never_reported_to_the_public(
    session_factory,
) -> None:
    """Un domaine mal résolu ne doit pas finir signalé « injoignable » au lecteur : on
    n'a pas demandé, donc on ne sait pas."""
    from app.services.liens_verification import signale

    async with session_factory() as session:
        await _lien(session, "https://insee.fr/a", "insee.fr")
        for _ in range(5):
            await verifier_les_liens(
                session, ClientFactice(), pause=0, resolveur=resolveur_interne
            )
        lien = await session.scalar(select(LienVerifie))
    assert lien.echecs_consecutifs == 0
    assert signale(lien) is False


async def test_the_pass_lists_before_it_checks(moderator_client, session_factory) -> None:
    """Un lien saisi entre deux passages doit être vu au suivant, sans qu'on ait à
    recenser à la main."""
    from tests.conftest import open_conversation

    conversation_id, _, _ = await open_conversation(
        moderator_client, statements=1, title="Recense puis verifie"
    )
    await moderator_client.post(
        f"/moderation/conversations/{conversation_id}/chiffres",
        data={"titre": "Un chiffre", "valeur": "1", "url": "https://insee.fr/neuf"},
        follow_redirects=False,
    )
    async with session_factory() as session:
        comptes = await verifier_les_liens(
            session, ClientFactice(code=200), pause=0, resolveur=resolveur_public
        )
    assert comptes == {"joignable": 1}


async def test_an_empty_catalogue_costs_nothing(session_factory) -> None:
    async with session_factory() as session:
        client = ClientFactice()
        assert await verifier_les_liens(session, client, pause=0) == {}
    assert client.appels == []
