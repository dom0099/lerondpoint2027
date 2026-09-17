"""L'affichage public de la vidéo de présentation (chantier Vidéo, VIDEO-4).

VIDEO-1/2/3 avaient transcodé, permis l'envoi, et laissé le résultat entièrement
invisible hors de l'écran de modération. Ce lot ouvre deux routes PUBLIQUES
(`/c/{slug}/video`, `/c/{slug}/video/couverture`), les branche sur la page d'un débat
et sur les cartes de liste, et ajoute une balise `og:image`.

Comme `tests/test_video_moderation.py`, ces tests exigent `ffmpeg`/`ffprobe` et se
sautent proprement si absents.
"""

from pathlib import Path

import pytest

from app.config import settings
from app.services import video
from tests.conftest import create_conversation, open_conversation
from tests.test_video import fabriquer

pytestmark = pytest.mark.skipif(
    not video.outils_disponibles(),
    reason="ffmpeg/ffprobe absents — dépendance SYSTÈME (apt install ffmpeg)",
)


@pytest.fixture(autouse=True)
def _media_temporaire(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "video_racine", str(tmp_path / "media"))
    yield


async def _attacher_et_choisir(moderator_client, conversation_id: int, tmp_path: Path):
    """Reproduit le geste du modérateur (VIDEO-2) : envoyer, puis choisir la
    miniature du milieu — sans quoi `video_miniature_chemin` reste `NULL`."""
    source = await fabriquer(tmp_path / "source.mp4", 6.0)
    with source.open("rb") as fichier:
        reponse = await moderator_client.post(
            f"/moderation/conversations/{conversation_id}/video",
            files={"fichier": (source.name, fichier, "video/mp4")},
            follow_redirects=False,
        )
    assert reponse.status_code == 303, reponse.text
    reponse = await moderator_client.post(
        f"/moderation/conversations/{conversation_id}/video/miniature",
        data={"indice": "1"},
        follow_redirects=False,
    )
    assert reponse.status_code == 303, reponse.text


async def test_video_visible_sur_la_page_du_debat_une_fois_choisie(
    moderator_client, client, tmp_path: Path
) -> None:
    conversation_id, slug, _ = await open_conversation(
        moderator_client, title="Un débat filmé"
    )
    await _attacher_et_choisir(moderator_client, conversation_id, tmp_path)

    page = await client.get(f"/c/{slug}")
    assert page.status_code == 200
    assert f"/c/{slug}/video" in page.text
    assert f"/c/{slug}/video/couverture" in page.text
    assert 'property="og:image"' in page.text
    assert f"/c/{slug}/video/couverture" in page.text.split('property="og:image"')[1][:200]

    fichier = await client.get(f"/c/{slug}/video")
    assert fichier.status_code == 200
    assert fichier.headers["content-type"] == "video/mp4"

    couverture = await client.get(f"/c/{slug}/video/couverture")
    assert couverture.status_code == 200
    assert couverture.headers["content-type"] == "image/jpeg"


async def test_pas_de_video_ni_og_image_sans_video(moderator_client, client) -> None:
    _, slug, _ = await open_conversation(moderator_client, title="Sans vidéo")

    page = await client.get(f"/c/{slug}")

    assert page.status_code == 200
    assert "og:image" not in page.text
    assert "miniature-video" not in page.text


async def test_video_non_choisie_reste_invisible_publiquement(
    moderator_client, client, tmp_path: Path
) -> None:
    """Vidéo attachée (VIDEO-2) mais miniature pas encore retenue : rien à montrer
    côté public — la couverture n'existe pas encore, et on ne fabrique pas d'`og:image`
    à partir d'un fichier qui n'est pas là."""
    conversation_id, slug, _ = await open_conversation(
        moderator_client, title="Vidéo pas encore choisie"
    )
    source = await fabriquer(tmp_path / "source.mp4", 6.0)
    with source.open("rb") as fichier:
        reponse = await moderator_client.post(
            f"/moderation/conversations/{conversation_id}/video",
            files={"fichier": (source.name, fichier, "video/mp4")},
            follow_redirects=False,
        )
    assert reponse.status_code == 303, reponse.text

    page = await client.get(f"/c/{slug}")
    assert "og:image" not in page.text
    assert "miniature-video-entete" not in page.text

    couverture = await client.get(f"/c/{slug}/video/couverture")
    assert couverture.status_code == 404


async def test_video_d_un_debat_non_publie_reste_404_en_public(
    moderator_client, client, tmp_path: Path
) -> None:
    conversation_id = await create_conversation(moderator_client, title="Brouillon")
    await _attacher_et_choisir(moderator_client, conversation_id, tmp_path)

    slug = "brouillon"
    assert (await client.get(f"/c/{slug}/video")).status_code == 404
    assert (await client.get(f"/c/{slug}/video/couverture")).status_code == 404


async def test_carte_de_liste_montre_la_miniature(
    moderator_client, client, tmp_path: Path
) -> None:
    conversation_id, slug, _ = await open_conversation(
        moderator_client, title="Débat en vitrine"
    )
    await _attacher_et_choisir(moderator_client, conversation_id, tmp_path)

    accueil = await client.get("/")
    assert f"/c/{slug}/video/couverture" in accueil.text

    debats = await client.get("/debats")
    assert f"/c/{slug}/video/couverture" in debats.text


async def test_la_route_video_publique_repond_aux_requetes_range(
    moderator_client, client, tmp_path: Path
) -> None:
    """Le lecteur natif du navigateur s'en sert pour avancer sans retélécharger tout
    le fichier — support intégré à `starlette.responses.FileResponse`, vérifié ici
    plutôt que supposé : aucune configuration nginx dédiée n'a semblé nécessaire
    (voir NOTES-CHANTIER-VIDEO.md), encore fallait-il le montrer sur la route
    réellement empruntée par un visiteur, pas seulement lire la doc de Starlette."""
    conversation_id, slug, _ = await open_conversation(
        moderator_client, title="Vidéo avec avance rapide"
    )
    await _attacher_et_choisir(moderator_client, conversation_id, tmp_path)

    entier = await client.get(f"/c/{slug}/video")
    assert entier.status_code == 200
    taille = len(entier.content)
    assert taille > 100

    partiel = await client.get(
        f"/c/{slug}/video", headers={"Range": f"bytes=0-{taille // 2}"}
    )
    assert partiel.status_code == 206
    assert partiel.headers["content-range"] == f"bytes 0-{taille // 2}/{taille}"
    assert len(partiel.content) == taille // 2 + 1


async def test_la_miniature_lance_la_video_et_se_partage(
    moderator_client, client, tmp_path: Path
) -> None:
    """Demande du client du 17/09/2026 : la miniature est un lien vers la vidéo
    (intercepté par `static/video.js` pour l'ouvrir en grand), la page porte un
    bouton de partage de l'adresse du DÉBAT, et la mention sous la vidéo a disparu."""
    conversation_id, slug, _ = await open_conversation(
        moderator_client, title="Débat à partager"
    )
    await _attacher_et_choisir(moderator_client, conversation_id, tmp_path)

    page = await client.get(f"/c/{slug}")
    assert f'class="lancer-video miniature-video" href="/c/{slug}/video"' in page.text
    assert 'class="partager-video"' in page.text
    assert f'data-partage="/c/{slug}"' in page.text
    assert "Vos droits sur cette vidéo" not in page.text
    assert "video.js" in page.text

    accueil = await client.get("/")
    assert f'class="lancer-video miniature-video-carte" href="/c/{slug}/video"' in accueil.text
