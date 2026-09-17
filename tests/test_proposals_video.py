"""La vidéo jointe AU MOMENT de la proposition d'un débat (chantier Vidéo, VIDEO-3).

Pas de page où revenir ensuite : l'envoi se fait dans le même POST que le reste du
formulaire /proposer, ou pas du tout — décision du 13/09/2026. La miniature est choisie
automatiquement (`preparer()`, pas l'écran à trois candidates du modérateur), et un
débat encore `pending` garde sa vidéo intacte : ni l'auteur (pas de retour possible),
ni le modérateur (tant qu'il n'a pas statué) ne peuvent la toucher.

Comme `tests/test_video.py` et `tests/test_video_moderation.py`, ces tests exigent
`ffmpeg` et se sautent proprement s'il manque.
"""

from pathlib import Path

import pytest
from sqlalchemy import select

from app import email as email_module
from app.config import settings
from app.models import Conversation
from app.services import video
from tests.test_video import fabriquer

pytestmark = pytest.mark.skipif(
    not video.outils_disponibles(),
    reason="ffmpeg/ffprobe absents — dépendance SYSTÈME (apt install ffmpeg)",
)

STATEMENTS = [
    "Les cantines devraient être gratuites.",
    "Chaque école doit avoir une cour végétalisée.",
    "Les devoirs à la maison sont à supprimer.",
]


@pytest.fixture(autouse=True)
def _media_temporaire(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "video_racine", str(tmp_path / "media"))
    yield


async def _proposer(client, video_chemin: Path | None, titre="Un débat avec vidéo"):
    data = {"title": titre, "description": "", "statements": STATEMENTS}
    if video_chemin is None:
        return await client.post("/proposer", data=data, follow_redirects=False)
    with video_chemin.open("rb") as fichier:
        return await client.post(
            "/proposer",
            data=data,
            files={"video": (video_chemin.name, fichier, "video/mp4")},
            follow_redirects=False,
        )


async def test_une_video_valide_est_jointe_a_la_proposition(
    client, tmp_path: Path, session_factory
) -> None:
    source = await fabriquer(tmp_path / "source.mp4", 6.0)

    reponse = await _proposer(client, source)

    assert reponse.status_code == 303, reponse.text
    async with session_factory() as session:
        conversation = await session.scalar(select(Conversation))
    assert conversation.moderation_status.value == "pending"
    assert conversation.video_chemin is not None
    # Miniature auto : PAS de candidates en attente, contrairement au parcours
    # modérateur — `preparer()`, pas `preparer_avec_choix_miniature()`.
    assert conversation.video_miniature_chemin is not None
    assert conversation.video_duree_secondes == pytest.approx(6.0, abs=0.5)


async def test_proposer_sans_video_fonctionne_toujours(
    client, session_factory
) -> None:
    """Non-régression : la vidéo est facultative, le formulaire d'origine marche
    encore exactement comme avant ce lot."""
    reponse = await _proposer(client, None)

    assert reponse.status_code == 303, reponse.text
    async with session_factory() as session:
        conversation = await session.scalar(select(Conversation))
    assert conversation.video_chemin is None


async def test_une_video_trop_longue_redonne_le_formulaire_sans_rien_creer(
    client, tmp_path: Path, session_factory
) -> None:
    trop_longue = await fabriquer(tmp_path / "trop-longue.mp4", 30.0)

    reponse = await _proposer(client, trop_longue, titre="Ce titre ne doit pas être perdu")

    assert reponse.status_code == 200
    assert "25" in reponse.text or "26" in reponse.text  # la limite, dite en clair
    # Le titre déjà tapé n'est pas perdu : même contrat que les autres erreurs de ce
    # formulaire (titre trop long, lien invalide, etc.).
    assert "Ce titre ne doit pas être perdu" in reponse.text
    async with session_factory() as session:
        assert await session.scalar(select(Conversation)) is None


async def test_un_fichier_au_dela_du_plafond_redonne_le_formulaire(
    client, tmp_path: Path, session_factory, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "video_max_octets", 1000)
    source = await fabriquer(tmp_path / "source.mp4", 6.0)
    assert source.stat().st_size > 1000

    reponse = await _proposer(client, source)

    assert reponse.status_code == 200
    assert "Mo" in reponse.text
    async with session_factory() as session:
        assert await session.scalar(select(Conversation)) is None


async def test_ffmpeg_absent_alerte_et_ne_bloque_pas_le_reste_du_site(
    client, tmp_path: Path, session_factory, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "alert_email", "astreinte@exemple.fr")

    async def _outil_absent(*args, **kwargs):
        raise video.OutilAbsent("ffmpeg est introuvable")

    monkeypatch.setattr(video, "preparer", _outil_absent)
    source = tmp_path / "peu-importe.mp4"
    source.write_bytes(b"peu importe, ffmpeg n'est jamais appele dans ce test")

    reponse = await _proposer(client, source)

    assert reponse.status_code == 200
    assert "sans vidéo" in reponse.text
    assert len(email_module.outbox) == 1
    assert email_module.outbox[0]["To"] == "astreinte@exemple.fr"
    async with session_factory() as session:
        assert await session.scalar(select(Conversation)) is None


async def test_un_anonyme_peut_joindre_une_video_comme_un_compte(
    client, tmp_path: Path, session_factory
) -> None:
    """Même contrat que le reste du formulaire : aucun compte exigé."""
    source = await fabriquer(tmp_path / "source.mp4", 5.0)

    reponse = await _proposer(client, source)

    assert reponse.status_code == 303, reponse.text
    async with session_factory() as session:
        conversation = await session.scalar(select(Conversation))
    assert conversation.video_chemin is not None
    assert conversation.owner_user_id is None


async def test_le_moderateur_ne_peut_pas_toucher_la_video_tant_que_pending(
    client, moderator_client, tmp_path: Path, session_factory
) -> None:
    source = await fabriquer(tmp_path / "source.mp4", 5.0)
    await _proposer(client, source)
    async with session_factory() as session:
        conversation = await session.scalar(select(Conversation))
    remplacement = await fabriquer(tmp_path / "remplacement.mp4", 4.0)

    with remplacement.open("rb") as fichier:
        reponse_envoi = await moderator_client.post(
            f"/moderation/conversations/{conversation.id}/video",
            files={"fichier": ("remplacement.mp4", fichier, "video/mp4")},
            follow_redirects=False,
        )
    reponse_suppression = await moderator_client.post(
        f"/moderation/conversations/{conversation.id}/video/supprimer",
        follow_redirects=False,
    )

    assert reponse_envoi.status_code == 409, reponse_envoi.text
    assert reponse_suppression.status_code == 409, reponse_suppression.text
    async with session_factory() as session:
        toujours_la = await session.get(Conversation, conversation.id)
        assert toujours_la.video_chemin == conversation.video_chemin


async def test_le_moderateur_reprend_la_main_apres_decision(
    client, moderator_client, tmp_path: Path, session_factory
) -> None:
    source = await fabriquer(tmp_path / "source.mp4", 5.0)
    await _proposer(client, source)
    async with session_factory() as session:
        conversation = await session.scalar(select(Conversation))

    approbation = await moderator_client.post(
        f"/moderation/queue/conversations/{conversation.id}/approve",
        data={"themes": ["institutions"]},
        follow_redirects=False,
    )
    assert approbation.status_code == 303, approbation.text

    reponse = await moderator_client.post(
        f"/moderation/conversations/{conversation.id}/video/supprimer",
        follow_redirects=False,
    )

    assert reponse.status_code == 303, reponse.text
    async with session_factory() as session:
        approuvee = await session.get(Conversation, conversation.id)
        assert approuvee.video_chemin is None
