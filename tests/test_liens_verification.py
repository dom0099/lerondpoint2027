"""K1 — la liste blanche, les verdicts, et l'état d'une adresse.

**Aucun test de ce fichier ne sort sur le réseau.** Le client HTTP est passé en argument
au vérificateur, et les tests lui donnent un client factice. C'est ce qui permet
d'éprouver les règles — la partie qui compte — sans dépendre d'un site tiers ni d'une
connexion, et sans que la suite devienne lente ou capricieuse.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import LienVerifie, Verdict
from app.services.liens_verification import (
    DOMAINES_AUTORISES,
    ECHECS_POUR_SIGNALER,
    JOURS_POUR_SIGNALER,
    Reponse,
    appliquer,
    classer,
    domaine_autorise,
    empreinte_de,
    normaliser,
    signale,
    verifier_une,
)

T0 = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


# --- la liste blanche --------------------------------------------------------------


@pytest.mark.parametrize(
    "hote",
    [
        "gouv.fr",
        "www.securite-routiere.gouv.fr",
        "www.sports.gouv.fr",
        "insee.fr",
        "www.senat.fr",
        "ec.europa.eu",
        "WWW.INSEE.FR",
        "www.insee.fr.",
    ],
)
def test_an_official_domain_is_allowed(hote: str) -> None:
    assert domaine_autorise(hote) is True


@pytest.mark.parametrize(
    "hote",
    [
        "notgouv.fr",
        "xgouv.fr",
        "insee.fr.attaquant.example",
        "gouv.fr.attaquant.example",
        "monsite.fr",
        "lemonde.fr",
        "",
    ],
)
def test_a_lookalike_domain_is_refused(hote: str) -> None:
    """Deux pièges, et le premier est la faute classique de toute liste blanche.

    `endswith("gouv.fr")` sans le point accepte `notgouv.fr` — la comparaison porte sur
    les ÉTIQUETTES du nom, jamais sur la chaîne. Et `gouv.fr.attaquant.example`
    **finit** par `attaquant.example` : c'est bien le suffixe qu'on teste, ce qui le
    rend inoffensif.
    """
    assert domaine_autorise(hote) is False


def test_the_whitelist_is_written_in_ascii() -> None:
    """Un domaine accentué est un IDN, qui ne s'écrit en DNS qu'en punycode : « sénat.fr »
    dans cette liste ne correspondrait à rien, et personne ne s'en apercevrait — la
    vérification se contenterait de ne jamais rien trouver."""
    for entree in DOMAINES_AUTORISES:
        assert entree.isascii(), entree
        assert entree == entree.lower()


def test_senat_and_the_assembly_are_both_there() -> None:
    """Les deux chambres n'ont pas de raison d'être traitées différemment ; `senat.fr`
    avait été oublié à la première rédaction de la liste."""
    assert domaine_autorise("www.senat.fr")
    assert domaine_autorise("www.assemblee-nationale.fr")


# --- la clé d'une adresse ----------------------------------------------------------


def test_the_fragment_does_not_make_a_new_address() -> None:
    """`#chapitre-3` désigne un endroit dans une page, pas une page. Deux chiffres citant
    deux sections d'un même rapport ne doivent pas faire interroger deux fois le même
    serveur."""
    a = "https://www.insee.fr/rapport#chapitre-3"
    b = "https://www.insee.fr/rapport#annexe"
    assert normaliser(a) == normaliser(b)
    assert empreinte_de(a) == empreinte_de(b)


def test_the_host_is_lowered_but_NOT_the_path() -> None:
    """Un chemin est sensible à la casse sur la plupart des serveurs : « normaliser »
    `/Rapport.pdf` en `/rapport.pdf` fabriquerait un 404 qui n'existe pas."""
    assert normaliser("https://WWW.INSEE.FR/Rapport.pdf") == "https://www.insee.fr/Rapport.pdf"
    assert empreinte_de("https://insee.fr/A") != empreinte_de("https://insee.fr/a")


def test_the_real_duplicate_of_production_collapses_to_one_entry() -> None:
    """Le doublon qui a décidé de la forme de la table : deux chiffres du débat sur le
    permis citent la même page."""
    url = ("https://www.securite-routiere.gouv.fr/les-chiffres-des-examens-du-permis"
           "-de-conduire/bilans-des-examens-du-permis")
    assert empreinte_de(url) == empreinte_de(url + "#bilan")


def test_the_fingerprint_is_short_enough_to_be_indexed() -> None:
    """La raison d'être de la colonne : un index B-tree PostgreSQL refuse une entrée de
    plus de ~2 700 octets, et `url` peut en faire 8 000 en UTF-8."""
    longue = "https://insee.fr/" + "a" * 1900
    assert len(empreinte_de(longue)) == 64


# --- les verdicts ------------------------------------------------------------------


@pytest.mark.parametrize("code", [404, 410])
def test_only_a_disappeared_page_is_unreachable(code: int) -> None:
    assert classer(Reponse(code=code), "insee.fr") is Verdict.injoignable


@pytest.mark.parametrize("code", [200, 204, 301, 302, 307, 308])
def test_a_redirect_is_not_a_death(code: int) -> None:
    """On ne SUIT pas les redirections (K2), mais une page qui redirige est une page qui
    répond."""
    assert classer(Reponse(code=code), "insee.fr") is Verdict.joignable


@pytest.mark.parametrize("code", [401, 403, 405, 429, 500, 502, 503])
def test_everything_else_is_an_I_DO_NOT_KNOW(code: int) -> None:
    """Le principe qui gouverne le chantier : un faux positif sur une source
    gouvernementale fait douter d'un chiffre juste. 403 dit que le site refuse les
    robots, 405 que HEAD ne lui plaît pas, 5xx qu'il va mal — aucun ne dit que la page
    est morte."""
    assert classer(Reponse(code=code), "insee.fr") is Verdict.non_concluant


def test_a_transport_failure_says_nothing_about_the_page() -> None:
    assert classer(Reponse(code=None, erreur="TimeoutError"), "insee.fr") is Verdict.non_concluant


def test_the_whitelist_is_checked_before_the_code() -> None:
    """Un domaine hors liste n'aurait jamais dû être interrogé : fonder un verdict sur
    une requête qui n'aurait pas dû partir serait le fonder sur une faute."""
    assert classer(Reponse(code=404), "lemonde.fr") is Verdict.hors_perimetre
    assert classer(Reponse(code=200), "lemonde.fr") is Verdict.hors_perimetre


# --- l'état, d'un passage à l'autre ------------------------------------------------


def _lien(url="https://www.insee.fr/rapport") -> LienVerifie:
    return LienVerifie(
        empreinte=empreinte_de(url), url=url, domaine="insee.fr",
        verdict=Verdict.non_verifie, echecs_consecutifs=0,
    )


def test_a_success_wipes_the_failure_streak() -> None:
    lien = _lien()
    appliquer(lien, Verdict.injoignable, 404, T0)
    appliquer(lien, Verdict.joignable, 200, T0 + timedelta(days=1))
    assert lien.echecs_consecutifs == 0
    assert lien.premier_echec is None
    assert lien.dernier_succes == T0 + timedelta(days=1)


def test_an_inconclusive_answer_neither_counts_nor_forgives() -> None:
    """La règle la plus subtile du fichier.

    Un site qui refuse les robots pendant trois semaines ne doit ni faire signaler ses
    pages, ni effacer la trace d'un 404 vu avant lui. Le compteur ne bouge donc pas — ni
    vers le haut, ni vers le bas.
    """
    lien = _lien()
    appliquer(lien, Verdict.injoignable, 404, T0)
    appliquer(lien, Verdict.non_concluant, 403, T0 + timedelta(days=8))
    assert lien.echecs_consecutifs == 1
    assert lien.premier_echec == T0


def test_the_first_failure_is_the_one_that_dates_the_streak() -> None:
    lien = _lien()
    appliquer(lien, Verdict.injoignable, 404, T0)
    appliquer(lien, Verdict.injoignable, 404, T0 + timedelta(days=9))
    assert lien.premier_echec == T0
    assert lien.dernier_essai == T0 + timedelta(days=9)
    assert lien.echecs_consecutifs == 2


# --- ce qui est DIT au public ------------------------------------------------------


def test_one_failure_says_nothing_publicly() -> None:
    """Une refonte de site un mardi matin ne doit pas faire crier au lien mort le mardi
    soir."""
    lien = _lien()
    appliquer(lien, Verdict.injoignable, 404, T0)
    assert signale(lien) is False


def test_two_failures_too_close_together_say_nothing_either() -> None:
    lien = _lien()
    appliquer(lien, Verdict.injoignable, 404, T0)
    appliquer(lien, Verdict.injoignable, 404, T0 + timedelta(days=1))
    assert lien.echecs_consecutifs == ECHECS_POUR_SIGNALER
    assert signale(lien) is False, "deux échecs, mais un seul jour d'écart"


def test_two_failures_a_week_apart_are_reported() -> None:
    lien = _lien()
    appliquer(lien, Verdict.injoignable, 404, T0)
    appliquer(lien, Verdict.injoignable, 404, T0 + timedelta(days=JOURS_POUR_SIGNALER))
    assert signale(lien) is True


def test_a_page_that_came_back_is_no_longer_reported() -> None:
    lien = _lien()
    appliquer(lien, Verdict.injoignable, 404, T0)
    appliquer(lien, Verdict.injoignable, 404, T0 + timedelta(days=10))
    assert signale(lien) is True
    appliquer(lien, Verdict.joignable, 200, T0 + timedelta(days=11))
    assert signale(lien) is False


@pytest.mark.parametrize(
    "verdict", [Verdict.non_verifie, Verdict.joignable, Verdict.non_concluant, Verdict.hors_perimetre]
)
def test_nothing_but_a_disappearance_is_ever_reported(verdict: Verdict) -> None:
    """« Non concluant » est enregistré pour nous, jamais montré au lecteur."""
    lien = _lien()
    lien.verdict = verdict
    lien.echecs_consecutifs = 99
    lien.premier_echec = T0 - timedelta(days=365)
    lien.dernier_essai = T0
    assert signale(lien) is False


# --- le vérificateur, avec un client factice ---------------------------------------


class ClientFactice:
    """Rend un code, ou lève. Aucune socket n'est ouverte."""

    def __init__(self, code=None, leve=None):
        self.code, self.leve, self.appels = code, leve, []

    async def head(self, url):
        self.appels.append(url)
        if self.leve is not None:
            raise self.leve
        return type("Reponse", (), {"status_code": self.code})()


async def test_the_verifier_reports_what_the_client_answered() -> None:
    lien = _lien()
    client = ClientFactice(code=404)
    assert await verifier_une(client, lien, T0) is Verdict.injoignable
    assert lien.code_http == 404
    assert client.appels == [lien.url]


async def test_a_transport_error_is_caught_and_says_nothing() -> None:
    """Un délai dépassé ne doit ni faire tomber le passage du worker, ni condamner une
    page."""
    lien = _lien()
    assert await verifier_une(ClientFactice(leve=TimeoutError()), lien, T0) is Verdict.non_concluant
    assert lien.code_http is None
    assert lien.echecs_consecutifs == 0


async def test_an_address_outside_the_whitelist_is_NEVER_requested() -> None:
    """C'est la liste blanche qui décide de sortir, pas la réponse qui décide après coup.

    Le test regarde l'absence d'appel, et non le verdict : un vérificateur qui
    interrogerait puis classerait « hors périmètre » aurait déjà fait la requête que le
    chantier existe pour éviter.
    """
    lien = _lien("https://lemonde.fr/article")
    client = ClientFactice(code=200)
    assert await verifier_une(client, lien, T0) is Verdict.hors_perimetre
    assert client.appels == [], "aucune requête ne doit partir"
