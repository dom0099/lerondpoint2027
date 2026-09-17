"""L'empreinte posée sur les adresses statiques, et la durée de cache qui va avec.

Le défaut corrigé était muet : `styles.css` et `detection.js` étaient liés par une
adresse fixe et servis sans `Cache-Control`, si bien qu'un navigateur pouvait
montrer pendant des jours une feuille ou des phrases que le site avait déjà
remplacées. Rien, côté serveur, ne permettait de s'en apercevoir — d'où ces tests,
qui sont la seule alarme possible.

Ce qui est éprouvé ici :

1. l'empreinte suit le CONTENU : même contenu, même adresse ; contenu changé,
   adresse changée, et cela **sans redémarrage** (le conteneur `api` monte `./app`
   en volume, un fichier modifié à chaud est un cas réel sur cette machine) ;
2. les trois liens des gabarits la portent ;
3. les deux régimes de cache, et le fait qu'une erreur n'en reçoit aucun ;
4. l'adresse NUE continue de fonctionner — c'est elle qui est écrite dans
   `styles.css` pour les polices, et dans les tests existants.
"""

from pathlib import Path

import pytest
from httpx import AsyncClient

from app.main import StatiquesEnCache
from app.services import statiques
from tests.conftest import open_conversation


# --------------------------------------------------------------------------------
# L'empreinte
# --------------------------------------------------------------------------------


def test_l_empreinte_est_stable_a_contenu_egal() -> None:
    assert statiques.url_statique("styles.css") == statiques.url_statique("styles.css")
    assert "?v=" in statiques.url_statique("styles.css")


def test_l_empreinte_change_quand_le_fichier_change(tmp_path: Path) -> None:
    """Le cœur du dispositif : c'est le contenu qui commande, pas le démarrage.

    Le test écrit vraiment sur le disque, dans un dossier temporaire substitué à
    `app/static`. Une version qui calculerait l'empreinte une fois pour toutes au
    démarrage passerait les autres tests et échouerait sur celui-ci.
    """
    fichier = tmp_path / "essai.css"
    fichier.write_text("a{}")
    dossier_reel = statiques.DOSSIER
    statiques.DOSSIER = tmp_path
    try:
        avant = statiques.empreinte("essai.css")
        # `st_mtime_ns` a une résolution fine, mais rien ne garantit qu'une seconde
        # écriture immédiate donne une date différente : on change aussi la taille,
        # l'autre moitié de la signature de fraîcheur.
        fichier.write_text("a{color:red}")
        apres = statiques.empreinte("essai.css")
    finally:
        statiques.DOSSIER = dossier_reel
        statiques._empreintes.pop("essai.css", None)
    assert avant is not None and apres is not None
    assert avant != apres


def test_un_fichier_absent_ne_fait_pas_tomber_la_page() -> None:
    """Une question de cache ne doit jamais coûter une page d'erreur."""
    assert statiques.empreinte("ce-fichier-n-existe-pas.css") is None
    assert statiques.url_statique("ce-fichier-n-existe-pas.css") == (
        "/static/ce-fichier-n-existe-pas.css"
    )


# --------------------------------------------------------------------------------
# Les liens des gabarits
# --------------------------------------------------------------------------------


async def test_l_accueil_lie_la_feuille_avec_son_empreinte(client: AsyncClient) -> None:
    page = (await client.get("/")).text
    assert f"/static/styles.css?v={statiques.empreinte('styles.css')}" in page


async def test_la_page_proposer_lie_le_script_avec_son_empreinte(
    client: AsyncClient,
) -> None:
    page = (await client.get("/proposer")).text
    assert f"/static/detection.js?v={statiques.empreinte('detection.js')}" in page


async def test_la_page_d_un_debat_lie_le_script_avec_son_empreinte(
    moderator_client, client: AsyncClient
) -> None:
    _, slug, _ = await open_conversation(moderator_client)
    page = (await client.get(f"/c/{slug}")).text
    assert f"/static/detection.js?v={statiques.empreinte('detection.js')}" in page


@pytest.mark.parametrize(
    "module",
    ["app.main", "app.routers.public", "app.routers.moderation"],
)
def test_chaque_instance_jinja_connait_url_statique(module: str) -> None:
    """Le projet a TROIS `Jinja2Templates`, et `public/base.html` — qui porte le
    lien de la feuille — est rendue par au moins deux d'entre elles.

    `public.py` rend les pages ordinaires ; `main.py` rend `indisponible.html`,
    la page servie quand la base ne répond pas. Une instance oubliée ne se verrait
    donc pas aux tests des pages courantes : elle donnerait une erreur Jinja le
    jour où la base est déjà tombée. D'où ce test, qui les vérifie toutes les
    trois plutôt que celles qu'on pense utiles.
    """
    import importlib

    templates = importlib.import_module(module).templates
    assert templates.env.globals.get("url_statique") is statiques.url_statique


# --------------------------------------------------------------------------------
# Les deux régimes de cache
# --------------------------------------------------------------------------------


async def test_une_adresse_avec_empreinte_se_cache_un_an(client: AsyncClient) -> None:
    reponse = await client.get(statiques.url_statique("styles.css"))
    assert reponse.status_code == 200
    assert reponse.headers["cache-control"] == StatiquesEnCache.CACHE_AVEC_EMPREINTE


@pytest.mark.parametrize(
    "adresse",
    [
        "/static/styles.css",
        "/static/detection.js",
        # Une police : jamais versionnée, parce que c'est `styles.css` qui l'appelle
        # en `url(...)` et non un gabarit.
        "/static/polices/archivo-latin-variable.woff2",
    ],
)
async def test_une_adresse_nue_marche_encore_et_se_cache_une_heure(
    client: AsyncClient, adresse: str
) -> None:
    reponse = await client.get(adresse)
    assert reponse.status_code == 200
    assert reponse.headers["cache-control"] == StatiquesEnCache.CACHE_SANS_EMPREINTE


async def test_une_erreur_ne_recoit_aucun_cache(client: AsyncClient) -> None:
    reponse = await client.get("/static/ce-fichier-n-existe-pas.css")
    assert reponse.status_code == 404
    assert "cache-control" not in reponse.headers
