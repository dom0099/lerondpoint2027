"""Le mécanisme vidéo : durée mesurée, transcodage, miniature, chemins en base.

Aucun écran n'est éprouvé ici, parce qu'il n'y en a pas : le VIDEO-1 construit le
mécanisme, le VIDEO-2 le branchera. Ce qui est éprouvé, c'est ce sur quoi tout le reste
reposera — qu'un fichier trop long soit refusé sur sa durée MESURÉE, que le fichier
produit soit lisible par un navigateur, et qu'aucun octet de vidéo n'entre en base.

**Aucun fichier binaire n'est versionné pour ces tests.** Les échantillons sont fabriqués
à la volée par `ffmpeg` : un dépôt ne doit pas grossir de quelques méga-octets à chaque
cas de test, et un `.mp4` committé serait invérifiable à la relecture. Le prix est que
ces tests exigent `ffmpeg` sur la machine — ils se sautent proprement s'il manque, plutôt
que de rougir pour une dépendance système absente.
"""

import struct
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Conversation
from app.services import video

pytestmark = pytest.mark.skipif(
    not video.outils_disponibles(),
    reason="ffmpeg/ffprobe absents — dépendance SYSTÈME (apt install ffmpeg)",
)


# --- fabrication d'échantillons --------------------------------------------------


async def fabriquer(chemin: Path, secondes: float, taille: str = "480x854") -> Path:
    """Un clip de synthèse, avec une piste son, de la durée demandée.

    De synthèse, et c'est assumé : ces tests éprouvent des RÈGLES (le seuil, le
    conteneur, la miniature), pas une qualité d'image. Ce qu'une mire ne saurait pas
    mesurer — le poids réel et le temps d'encodage d'un vrai enregistrement — est
    justement le travail du banc, qui tourne sur un corpus de vraies prises de vue.
    """
    code, _, erreurs = await video._executer(
        "ffmpeg",
        "-nostdin", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc=size={taille}:rate=30:duration={secondes}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={secondes}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest",
        str(chemin),
    )
    assert code == 0, erreurs.decode("utf-8", "replace")[-400:]
    return chemin


def boites_de_tete(mp4: Path) -> list[str]:
    """Les noms des boîtes de premier niveau d'un `.mp4`, dans l'ordre du fichier.

    Un `.mp4` est une suite de boîtes « taille (4 octets) + nom (4 octets) + contenu ».
    Lire l'ordre réel demande donc huit octets par boîte et rien d'autre.

    **Pourquoi pas ffprobe.** ffprobe dit ce que le fichier CONTIENT, pas dans quel ordre
    il le range : il lit le `moov` où qu'il soit, et rend exactement la même description
    avec ou sans `faststart`. Or c'est précisément l'ORDRE qui est en question — le
    drapeau ne change rien d'autre.
    """
    noms: list[str] = []
    with mp4.open("rb") as fichier:
        position = 0
        taille_fichier = mp4.stat().st_size
        while position < taille_fichier:
            fichier.seek(position)
            entete = fichier.read(8)
            if len(entete) < 8:
                break
            taille, nom = struct.unpack(">I4s", entete)
            noms.append(nom.decode("ascii", "replace"))
            if taille == 1:  # taille sur 64 bits, rangée juste après le nom
                taille = struct.unpack(">Q", fichier.read(8))[0]
            elif taille == 0:  # « jusqu'à la fin du fichier »
                break
            if taille < 8:
                break
            position += taille
    return noms


# --- la durée, mesurée et non déclarée -------------------------------------------


async def test_une_video_trop_longue_est_refusee(tmp_path: Path) -> None:
    """Au-delà du seuil, le pipeline refuse — et dit de combien il s'agit.

    Le fichier ne porte ni durée dans son nom, ni métadonnée avantageuse : c'est
    `ffprobe` qui mesure, et c'est la seule chose qui décide.
    """
    trop_long = await fabriquer(tmp_path / "trop-long.mp4", video.SEUIL_REJET_SECONDES + 4)

    with pytest.raises(video.VideoTropLongue) as refus:
        await video.verifier_duree(trop_long)

    assert refus.value.duree > video.SEUIL_REJET_SECONDES
    assert "25" in str(refus.value)


async def test_un_nom_de_fichier_mensonger_ne_trompe_personne(tmp_path: Path) -> None:
    """Le nom du fichier vient du client. Il ne vaut rien, et ne doit rien décider."""
    menteur = await fabriquer(tmp_path / "duree-10-secondes.mp4", 30.0)

    with pytest.raises(video.VideoTropLongue):
        await video.verifier_duree(menteur)


async def test_juste_sous_le_seuil_la_video_passe(tmp_path: Path) -> None:
    """La marge d'arrondi existe pour servir : 25,4 s doit passer, pas échouer.

    Sans elle, une vidéo de 25 s rendue par un téléphone à 25,04 s serait refusée pour
    une raison que la personne ne peut ni voir ni corriger.
    """
    limite = await fabriquer(tmp_path / "limite.mp4", video.DUREE_MAX_SECONDES + 0.4)

    sonde = await video.verifier_duree(limite)

    assert video.DUREE_MAX_SECONDES < sonde.duree_s <= video.SEUIL_REJET_SECONDES


# --- le fichier produit ----------------------------------------------------------


async def test_le_fichier_produit_est_h264_aac_en_mp4(tmp_path: Path) -> None:
    """Le format universel, quel que soit ce qui entre — c'est tout l'objet du lot."""
    source = await fabriquer(tmp_path / "source.mp4", 6.0)

    resultat = await video.preparer(source, racine=tmp_path / "media")

    produit = tmp_path / "media" / resultat.chemin_video
    assert produit.suffix == ".mp4"
    sonde = await video.sonder(produit)
    assert sonde.codec_video == "h264"
    assert sonde.codec_audio == "aac"


async def test_le_moov_precede_le_mdat(tmp_path: Path) -> None:
    """`faststart` : les métadonnées en tête, les données ensuite.

    Sans cet ordre, un lecteur doit avoir reçu la fin du fichier avant de montrer la
    première image — et l'aperçu de partage du VIDEO-3 n'afficherait rien du tout.
    """
    source = await fabriquer(tmp_path / "source.mp4", 6.0)

    resultat = await video.preparer(source, racine=tmp_path / "media")

    boites = boites_de_tete(tmp_path / "media" / resultat.chemin_video)
    assert "moov" in boites and "mdat" in boites
    assert boites.index("moov") < boites.index("mdat"), boites


async def test_une_video_sans_piste_son_passe_quand_meme(tmp_path: Path) -> None:
    """Une vidéo muette est une vidéo. Elle n'a pas à faire échouer l'encodage.

    Le cas arrive pour de bon : capture d'écran, appareil au micro coupé, montage
    exporté sans son. La commande d'encodage demande de l'AAC sans condition — il faut
    donc s'assurer qu'elle ne se fâche pas quand il n'y a rien à encoder.
    """
    muette = tmp_path / "muette.mp4"
    code, _, erreurs = await video._executer(
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=size=480x854:rate=30:duration=5",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(muette),
    )
    assert code == 0, erreurs.decode("utf-8", "replace")[-400:]

    resultat = await video.preparer(muette, racine=tmp_path / "media")

    produit = await video.sonder(tmp_path / "media" / resultat.chemin_video)
    assert produit.codec_video == "h264"
    assert produit.codec_audio is None


async def test_le_cadre_ne_depasse_pas_le_profil_et_reste_dans_le_bon_sens() -> None:
    """La borne du profil s'applique selon l'orientation, et n'agrandit jamais."""
    profil = video.PROFIL_ECONOMIQUE

    assert video.dimensions_cibles(1080, 1920, profil) == (480, 854)
    assert video.dimensions_cibles(1920, 1080, profil) == (854, 480)
    assert video.dimensions_cibles(1000, 1000, profil) == (480, 480)
    # Plus petit que la borne : rendu tel quel. Agrandir n'ajoute aucun détail, coûte
    # du débit, et rend le flou plus visible.
    assert video.dimensions_cibles(320, 240, profil) == (320, 240)
    # Toujours pair : H.264 en 4:2:0 refuse une dimension impaire.
    largeur, hauteur = video.dimensions_cibles(1079, 1921, profil)
    assert largeur % 2 == 0 and hauteur % 2 == 0


async def test_une_video_couchee_a_matrice_de_rotation_sort_en_portrait(
    tmp_path: Path,
) -> None:
    """Le piège de l'iPhone : 1920x1080 dans le fichier, portrait à l'écran.

    Lire `width`/`height` sans regarder la matrice de rotation donnerait un cadre de
    sortie couché à la moitié des vidéos de téléphone.
    """
    couchee = await fabriquer(tmp_path / "couchee.mp4", 5.0, taille="854x480")
    tournee = tmp_path / "tournee.mov"
    code, _, erreurs = await video._executer(
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-display_rotation", "90", "-i", str(couchee),
        "-map", "0", "-c", "copy", str(tournee),
    )
    assert code == 0, erreurs.decode("utf-8", "replace")[-400:]

    sonde = await video.sonder(tournee)
    assert sonde.est_portrait, "la rotation n'a pas été lue"

    resultat = await video.preparer(tournee, racine=tmp_path / "media")
    assert resultat.hauteur > resultat.largeur


# --- la miniature ----------------------------------------------------------------


async def test_la_miniature_est_produite_et_non_vide(tmp_path: Path) -> None:
    source = await fabriquer(tmp_path / "source.mp4", 6.0)

    resultat = await video.preparer(source, racine=tmp_path / "media")

    miniature = tmp_path / "media" / resultat.chemin_miniature
    assert miniature.suffix == ".jpg"
    assert miniature.stat().st_size > 0
    # Un JPEG commence par SOI (0xFFD8) : un fichier non vide mais tronqué passerait
    # une simple vérification de taille.
    assert miniature.read_bytes()[:2] == b"\xff\xd8"


# --- le choix de miniature (VIDEO-2) ----------------------------------------------


async def test_trois_candidates_sont_produites_et_distinctes(tmp_path: Path) -> None:
    source = await fabriquer(tmp_path / "source.mp4", 6.0)
    media = tmp_path / "media"

    resultat = await video.preparer_avec_choix_miniature(source, racine=media)

    base = Path(resultat.chemin_video).stem
    dossier = media / Path(resultat.chemin_video).parent
    candidates = [dossier / f"{base}-choix-{i}.jpg" for i in range(3)]
    tailles = set()
    for candidate in candidates:
        assert candidate.suffix == ".jpg"
        assert candidate.stat().st_size > 0
        assert candidate.read_bytes()[:2] == b"\xff\xd8"
        tailles.add(candidate.read_bytes())
    # Prises à des instants différents d'une mire qui change dans le temps : les trois
    # fichiers ne sont pas des copies l'un de l'autre.
    assert len(tailles) == 3


async def test_choisir_miniature_retient_une_candidate_et_efface_les_autres(
    tmp_path: Path,
) -> None:
    source = await fabriquer(tmp_path / "source.mp4", 6.0)
    media = tmp_path / "media"
    resultat = await video.preparer_avec_choix_miniature(source, racine=media)
    base = Path(resultat.chemin_video).stem
    dossier = media / Path(resultat.chemin_video).parent

    relatif_miniature = video.choisir_miniature(
        resultat.chemin_video, 1, racine=media
    )

    assert relatif_miniature == str((dossier / f"{base}.jpg").relative_to(media))
    assert (dossier / f"{base}.jpg").is_file()
    for i in range(3):
        assert not (dossier / f"{base}-choix-{i}.jpg").is_file()


async def test_choisir_miniature_hors_bornes_est_refuse(tmp_path: Path) -> None:
    source = await fabriquer(tmp_path / "source.mp4", 6.0)
    media = tmp_path / "media"
    resultat = await video.preparer_avec_choix_miniature(source, racine=media)

    with pytest.raises(video.ErreurVideo):
        video.choisir_miniature(resultat.chemin_video, 3, racine=media)


async def test_supprimer_candidates_miniatures_efface_les_trois(tmp_path: Path) -> None:
    source = await fabriquer(tmp_path / "source.mp4", 6.0)
    media = tmp_path / "media"
    resultat = await video.preparer_avec_choix_miniature(source, racine=media)
    base = Path(resultat.chemin_video).stem
    dossier = media / Path(resultat.chemin_video).parent

    video.supprimer_candidates_miniatures(resultat.chemin_video, racine=media)

    for i in range(3):
        assert not (dossier / f"{base}-choix-{i}.jpg").is_file()
    # La vidéo elle-même n'est pas touchée : seules les candidates le sont.
    assert (media / resultat.chemin_video).is_file()


async def test_un_echec_de_choix_de_miniature_ne_laisse_rien_derriere(
    tmp_path: Path,
) -> None:
    """Même garantie que `preparer()` : un fichier trop long ne laisse ni vidéo ni
    candidate sur le disque."""
    media = tmp_path / "media"
    media.mkdir()
    source = await fabriquer(tmp_path / "trop-longue.mp4", 30.0)

    with pytest.raises(video.VideoTropLongue):
        await video.preparer_avec_choix_miniature(source, racine=media)

    assert list(media.iterdir()) == []


# --- la réception, plafonnée en flux (VIDEO-2/3) -----------------------------------


def _lecteur(morceaux: list[bytes]):
    """Un faux `UploadFile.read` : rend les morceaux donnés puis b"" (fin du flux)."""
    reste = list(morceaux)

    async def lire(taille: int) -> bytes:
        return reste.pop(0) if reste else b""

    return lire


async def test_recevoir_ecrit_le_flux_recu(tmp_path: Path) -> None:
    lecture = _lecteur([b"un", b"deux", b"trois"])

    chemin = await video.recevoir(lecture, plafond_octets=1000, suffixe=".mp4")

    assert chemin.suffix == ".mp4"
    assert chemin.read_bytes() == b"undeuxtrois"
    chemin.unlink()


async def test_recevoir_refuse_au_dela_du_plafond_et_ne_laisse_rien(tmp_path: Path) -> None:
    # Impossible de nommer le fichier temporaire d'avance (`mkstemp` tire un nom au
    # hasard) : on compare l'état du dossier système avant/après au lieu d'un chemin.
    avant = set(Path(tempfile.gettempdir()).glob("video-recue-*"))
    lecture = _lecteur([b"x" * 600, b"x" * 600])

    with pytest.raises(video.FichierTropGros) as excinfo:
        await video.recevoir(lecture, plafond_octets=1000)

    assert excinfo.value.plafond == 1000
    assert excinfo.value.octets_recus == 1200
    apres = set(Path(tempfile.gettempdir()).glob("video-recue-*"))
    assert apres == avant


# --- les erreurs -----------------------------------------------------------------


async def test_un_fichier_corrompu_leve_une_erreur_nommee(tmp_path: Path) -> None:
    """Une erreur claire, jamais un plantage silencieux ni un fichier vide servi."""
    corrompu = tmp_path / "corrompu.mp4"
    corrompu.write_bytes(b"\x00\x01\x02 ceci n'est pas une video" * 50)

    with pytest.raises(video.FichierIllisible):
        await video.sonder(corrompu)


async def test_un_fichier_d_un_autre_type_est_refuse(tmp_path: Path) -> None:
    """Un texte renommé en `.mp4` : le format n'est pas dans l'extension."""
    faux = tmp_path / "document.mp4"
    faux.write_text("Bonjour, je suis un fichier texte.\n" * 100, encoding="utf-8")

    with pytest.raises(video.FichierIllisible):
        await video.verifier_duree(faux)


async def test_un_son_sans_image_est_refuse(tmp_path: Path) -> None:
    """Un fichier parfaitement lisible, mais sans piste vidéo : ce n'est pas une vidéo."""
    son = tmp_path / "son.m4a"
    code, _, _ = await video._executer(
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-c:a", "aac", str(son),
    )
    assert code == 0

    with pytest.raises(video.FichierIllisible, match="piste vidéo"):
        await video.sonder(son)


async def test_un_echec_ne_laisse_pas_de_fichier_orphelin(tmp_path: Path) -> None:
    """Un `.mp4` sans sa miniature finirait par être servi un jour, sans image."""
    media = tmp_path / "media"
    media.mkdir()
    vide = tmp_path / "vide.mp4"
    vide.write_bytes(b"")

    with pytest.raises(video.ErreurVideo):
        await video.preparer(vide, racine=media)

    assert list(media.iterdir()) == []


async def test_un_chemin_qui_sort_du_repertoire_des_medias_est_refuse(
    tmp_path: Path,
) -> None:
    """`supprimer()` efface. Un `..` glissé en base viserait un fichier du VPS."""
    media = tmp_path / "media"
    media.mkdir()
    voisin = tmp_path / "a-ne-pas-effacer.txt"
    voisin.write_text("intact", encoding="utf-8")

    with pytest.raises(video.ErreurVideo):
        video.supprimer("../a-ne-pas-effacer.txt", racine=media)

    assert voisin.exists()


# --- la base ne contient que des chemins -----------------------------------------


async def test_la_base_ne_recoit_que_des_chemins_jamais_des_octets(
    tmp_path: Path, session_factory
) -> None:
    """Le principe des liens-source du chantier J, appliqué à un binaire.

    On vérifie les deux faces : ce qui est écrit EST le chemin, et la taille totale de
    la ligne reste sans commune mesure avec celle du fichier. Une colonne binaire
    ajoutée un jour par commodité ferait rougir ce test.
    """
    source = await fabriquer(tmp_path / "source.mp4", 6.0)
    media = tmp_path / "media"
    resultat = await video.preparer(source, racine=media)

    async with session_factory() as session:
        debat = Conversation(slug="video-1", title="Un débat avec une vidéo")
        session.add(debat)
        await session.flush()
        debat.video_chemin = resultat.chemin_video
        debat.video_miniature_chemin = resultat.chemin_miniature
        debat.video_duree_secondes = resultat.duree_s
        debat.video_profil = resultat.profil
        await session.commit()
        identifiant = debat.id

    async with session_factory() as session:
        relu = await session.scalar(
            select(Conversation).where(Conversation.id == identifiant)
        )
        assert relu.video_chemin == resultat.chemin_video
        assert relu.video_chemin.endswith(".mp4")
        assert relu.video_profil == video.PROFIL_ECONOMIQUE.nom
        assert relu.video_duree_secondes == pytest.approx(resultat.duree_s, abs=0.05)

        # Le fichier pèse ses octets sur le disque ; la ligne en base pèse la longueur
        # de deux chemins. C'est la différence qu'on protège.
        octets_en_base = sum(
            len(str(valeur or "").encode())
            for valeur in (relu.video_chemin, relu.video_miniature_chemin, relu.video_profil)
        )
        assert octets_en_base < 500
        assert (media / relu.video_chemin).stat().st_size > 10_000


async def test_un_debat_sans_video_reste_intact(session_factory) -> None:
    """Le cas normal, et celui de tous les débats déjà en base : quatre NULL."""
    async with session_factory() as session:
        debat = Conversation(slug="sans-video", title="Un débat ordinaire")
        session.add(debat)
        await session.commit()
        identifiant = debat.id

    async with session_factory() as session:
        relu = await session.scalar(
            select(Conversation).where(Conversation.id == identifiant)
        )
        assert relu.video_chemin is None
        assert relu.video_miniature_chemin is None
        assert relu.video_duree_secondes is None
        assert relu.video_profil is None


# --- le profil, à un seul endroit -------------------------------------------------


def test_le_profil_est_nomme_et_relisible_depuis_la_base() -> None:
    """La colonne `video_profil` porte un nom ; ce nom doit retrouver ses réglages."""
    assert video.PROFILS[video.PROFIL_ECONOMIQUE.nom] is video.PROFIL_ECONOMIQUE
    assert video.PROFIL_PAR_DEFAUT.debit_total_kbps == pytest.approx(800, abs=10)


def test_le_seuil_de_rejet_derive_de_la_duree_annoncee() -> None:
    """Un seul endroit décide : le seuil est une somme, pas un second nombre écrit à part.

    C'est ce qui interdit qu'on change la durée maximale un jour sans que le seuil de
    rejet suive — le genre d'écart qui ne fait rougir aucun test tant qu'on l'écrit deux
    fois.
    """
    assert video.SEUIL_REJET_SECONDES == (
        video.DUREE_MAX_SECONDES + video.TOLERANCE_DUREE_SECONDES
    )
