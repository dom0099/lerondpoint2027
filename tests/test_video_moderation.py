"""L'écran de modération qui attache une vidéo (VIDEO-2).

VIDEO-1 avait laissé une question ouverte : « d'où viennent les envois ? ». La réponse
tranchée le 13/09/2026 est ici — l'éditeur de conversation, réservé aux modérateurs,
appelé en ligne (pas de file d'attente : l'encodage tient en quelques secondes).

Comme `tests/test_video.py`, ces tests exigent `ffmpeg` sur la machine et se sautent
proprement s'il manque.
"""

from pathlib import Path

import pytest

from app import email as email_module
from app.config import settings
from app.models import Conversation
from app.services import video
from tests.conftest import create_conversation
from tests.test_video import fabriquer

pytestmark = pytest.mark.skipif(
    not video.outils_disponibles(),
    reason="ffmpeg/ffprobe absents — dépendance SYSTÈME (apt install ffmpeg)",
)


@pytest.fixture(autouse=True)
def _media_temporaire(tmp_path, monkeypatch):
    """Chaque test écrit dans SON répertoire, jamais dans `media/videos` du dépôt."""
    monkeypatch.setattr(settings, "video_racine", str(tmp_path / "media"))
    yield


async def _envoyer(moderator_client, conversation_id: int, chemin: Path, **kwargs):
    with chemin.open("rb") as fichier:
        return await moderator_client.post(
            f"/moderation/conversations/{conversation_id}/video",
            files={"fichier": (chemin.name, fichier, "video/mp4")},
            follow_redirects=False,
            **kwargs,
        )


async def test_un_envoi_valide_attache_la_video_et_trois_candidates(
    moderator_client, tmp_path: Path
) -> None:
    conversation_id = await create_conversation(moderator_client)
    source = await fabriquer(tmp_path / "source.mp4", 6.0)

    reponse = await _envoyer(moderator_client, conversation_id, source)

    assert reponse.status_code == 303, reponse.text
    page = await moderator_client.get(f"/moderation/conversations/{conversation_id}")
    assert page.status_code == 200
    assert "Retenir cette miniature" in page.text

    for i in range(3):
        candidate = await moderator_client.get(
            f"/moderation/conversations/{conversation_id}/video/miniature/{i}"
        )
        assert candidate.status_code == 200
        assert candidate.headers["content-type"] == "image/jpeg"

    fichier_video = await moderator_client.get(
        f"/moderation/conversations/{conversation_id}/video/fichier"
    )
    assert fichier_video.status_code == 200
    assert fichier_video.headers["content-type"] == "video/mp4"


async def test_choisir_une_miniature_la_rend_disponible_en_couverture(
    moderator_client, tmp_path: Path
) -> None:
    conversation_id = await create_conversation(moderator_client)
    source = await fabriquer(tmp_path / "source.mp4", 6.0)
    await _envoyer(moderator_client, conversation_id, source)

    reponse = await moderator_client.post(
        f"/moderation/conversations/{conversation_id}/video/miniature",
        data={"indice": "2"},
        follow_redirects=False,
    )
    assert reponse.status_code == 303, reponse.text

    couverture = await moderator_client.get(
        f"/moderation/conversations/{conversation_id}/video/couverture"
    )
    assert couverture.status_code == 200

    # Les candidates non retenues ont disparu.
    for i in (0, 1):
        candidate = await moderator_client.get(
            f"/moderation/conversations/{conversation_id}/video/miniature/{i}"
        )
        assert candidate.status_code == 404


async def test_un_fichier_trop_long_est_refuse_sans_rien_attacher(
    moderator_client, tmp_path: Path
) -> None:
    conversation_id = await create_conversation(moderator_client)
    trop_longue = await fabriquer(tmp_path / "trop-longue.mp4", 30.0)

    reponse = await _envoyer(moderator_client, conversation_id, trop_longue)

    assert reponse.status_code == 422, reponse.text
    fichier_video = await moderator_client.get(
        f"/moderation/conversations/{conversation_id}/video/fichier"
    )
    assert fichier_video.status_code == 404


async def test_un_envoi_au_dela_du_plafond_en_octets_est_refuse(
    moderator_client, tmp_path: Path, monkeypatch
) -> None:
    """Décision du 13/09/2026 : 200 Mo par défaut — ici abaissé pour ne pas fabriquer
    un fichier de cette taille juste pour le test."""
    monkeypatch.setattr(settings, "video_max_octets", 1000)
    conversation_id = await create_conversation(moderator_client)
    source = await fabriquer(tmp_path / "source.mp4", 6.0)
    assert source.stat().st_size > 1000

    reponse = await _envoyer(moderator_client, conversation_id, source)

    assert reponse.status_code == 413, reponse.text
    assert "Mo" in reponse.text


async def test_remplacer_une_video_efface_l_ancienne_et_ses_candidates(
    moderator_client, tmp_path: Path, session_factory
) -> None:
    conversation_id = await create_conversation(moderator_client)
    premiere = await fabriquer(tmp_path / "premiere.mp4", 5.0)
    seconde = await fabriquer(tmp_path / "seconde.mp4", 7.0)

    await _envoyer(moderator_client, conversation_id, premiere)
    async with session_factory() as session:
        debat = await session.get(Conversation, conversation_id)
        ancien_chemin = debat.video_chemin
    racine = Path(settings.video_racine).expanduser()
    ancienne_video = racine / ancien_chemin
    assert ancienne_video.is_file()

    await _envoyer(moderator_client, conversation_id, seconde)

    assert not ancienne_video.is_file()
    async with session_factory() as session:
        debat = await session.get(Conversation, conversation_id)
        assert debat.video_chemin != ancien_chemin
        assert debat.video_duree_secondes == pytest.approx(7.0, abs=0.5)


async def test_retirer_une_video_efface_les_fichiers_et_les_colonnes(
    moderator_client, tmp_path: Path, session_factory
) -> None:
    conversation_id = await create_conversation(moderator_client)
    source = await fabriquer(tmp_path / "source.mp4", 5.0)
    await _envoyer(moderator_client, conversation_id, source)
    async with session_factory() as session:
        debat = await session.get(Conversation, conversation_id)
        chemin_video = Path(settings.video_racine).expanduser() / debat.video_chemin

    reponse = await moderator_client.post(
        f"/moderation/conversations/{conversation_id}/video/supprimer",
        follow_redirects=False,
    )

    assert reponse.status_code == 303, reponse.text
    assert not chemin_video.is_file()
    async with session_factory() as session:
        debat = await session.get(Conversation, conversation_id)
        assert debat.video_chemin is None
        assert debat.video_miniature_chemin is None
        assert debat.video_duree_secondes is None
        assert debat.video_profil is None


async def test_ffmpeg_absent_declenche_une_alerte_par_courriel(
    moderator_client, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "alert_email", "astreinte@exemple.fr")

    async def _outil_absent(*args, **kwargs):
        raise video.OutilAbsent("ffmpeg est introuvable")

    monkeypatch.setattr(video, "preparer_avec_choix_miniature", _outil_absent)

    conversation_id = await create_conversation(moderator_client)
    source = tmp_path / "peu-importe.mp4"
    source.write_bytes(b"peu importe, ffmpeg n'est jamais appele dans ce test")

    reponse = await _envoyer(moderator_client, conversation_id, source)

    assert reponse.status_code == 503, reponse.text
    assert len(email_module.outbox) == 1
    message = email_module.outbox[0]
    assert message["To"] == "astreinte@exemple.fr"
    assert "ffmpeg" in message.get_content()


async def test_sans_adresse_d_alerte_aucun_courriel_n_est_tente(
    moderator_client, tmp_path: Path, monkeypatch
) -> None:
    """`alert_email` vide (défaut) : le journal suffit, aucun envoi n'est tenté."""
    monkeypatch.setattr(settings, "alert_email", "")

    async def _outil_absent(*args, **kwargs):
        raise video.OutilAbsent("ffmpeg est introuvable")

    monkeypatch.setattr(video, "preparer_avec_choix_miniature", _outil_absent)

    conversation_id = await create_conversation(moderator_client)
    source = tmp_path / "peu-importe.mp4"
    source.write_bytes(b"peu importe")

    reponse = await _envoyer(moderator_client, conversation_id, source)

    assert reponse.status_code == 503
    assert email_module.outbox == []
