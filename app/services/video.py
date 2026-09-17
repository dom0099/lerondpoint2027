"""Vidéo de présentation d'un débat — vérification, transcodage, miniature (VIDEO-1).

Une personne qui propose un débat peut y joindre une courte vidéo de présentation.
Ce module est le mécanisme qui la reçoit : il ne connaît ni formulaire, ni gabarit, ni
file de modération. On lui donne un fichier, il rend un `.mp4` servable et une image de
couverture — ou une erreur nommée.

Trois règles gouvernent tout le fichier.

  1. **Le transcodage n'est jamais optionnel.** Même un fichier qui semble déjà correct
     repasse par `ffmpeg`. Un téléphone enregistre en HEVC dans un `.mov` (iPhone), en
     4K, à 20 Mbit/s, parfois avec une matrice de rotation plutôt qu'une image droite :
     rien de tout cela n'est lisible par tous les navigateurs, et rien de tout cela ne
     se devine à l'extension. Servir l'original, c'est servir ce qu'on n'a pas regardé.
  2. **Aucune donnée venue du client n'est crue.** Ni la durée annoncée, ni le type
     MIME, ni l'extension. La durée est CELLE QUE `ffprobe` MESURE, et c'est la seule
     qui décide du rejet. Le chronomètre du navigateur qui viendra au VIDEO-2 est un
     confort d'ergonomie, pas un contrôle.
  3. **Le binaire ne va jamais en base.** Un chemin y va, le fichier va sur le disque —
     exactement comme les liens-source du chantier J stockent une URL et pas une page.

**Pourquoi asynchrone.** `ffmpeg` est appelé par `asyncio.create_subprocess_exec` et non
par `subprocess.run` : le reste de l'application partage une boucle d'événements, et un
encodage qui dure des secondes la bloquerait entièrement. Ce lot n'appelle le pipeline
que depuis la CLI, où cela ne changerait rien — mais le VIDEO-2 l'appellera depuis le
worker, et c'est là que la différence se paie.
"""

import asyncio
import json
import logging
import math
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

logger = logging.getLogger("app.video")


# --------------------------------------------------------------------------------
# Les réglages — ici, et nulle part ailleurs
# --------------------------------------------------------------------------------
# Le banc de mesure (`python -m app.cli banc-video`) sert à revoir ces nombres après
# coup. Il ne le peut que s'ils vivent à UN seul endroit : une constante dupliquée dans
# une commande `ffmpeg` et dans un test est une constante qu'on n'ajustera jamais.
#
# Ces valeurs-ci sont des décisions de produit, pas de déploiement : elles ne dépendent
# pas de la machine, et les changer change ce que voient les participants. Elles restent
# donc des constantes de module, quand `settings.video_racine` — qui, lui, diffère d'un
# poste à l'autre et du VPS — est bien un réglage d'environnement.

#: Durée maximale annoncée aux participants, en secondes.
DUREE_MAX_SECONDES = 25.0

#: Marge acceptée au-dessus de `DUREE_MAX_SECONDES`. Une seconde, et le motif n'est pas
#: la politesse : un encodeur ne coupe qu'aux frontières d'images, la durée d'un conteneur
#: se lit à la milliseconde près, et un téléphone qui vise 25 s en rend couramment 25,04.
#: Rejeter à 25,000 s exactement ferait échouer des vidéos conformes pour une erreur
#: d'arrondi — donc pour une raison que la personne ne peut ni voir ni corriger.
TOLERANCE_DUREE_SECONDES = 1.0

#: Le seuil effectif du rejet. C'est lui qu'on compare, jamais `DUREE_MAX_SECONDES` seul.
SEUIL_REJET_SECONDES = DUREE_MAX_SECONDES + TOLERANCE_DUREE_SECONDES

#: Plafond d'images par seconde. Un téléphone filme volontiers à 60 i/s ; à 700 kbit/s,
#: doubler le nombre d'images revient à diviser par deux ce que chacune reçoit, et le
#: résultat est visiblement plus sale qu'à 30. Pour une personne qui parle face caméra,
#: 30 i/s ne coûtent rien de perceptible.
IMAGES_PAR_SECONDE_MAX = 30

#: Instant de la miniature, en part de la durée. À la moitié plutôt qu'au début : la
#: première image d'une vidéo de téléphone est souvent un flou de mise au point, ou le
#: plafond, parce que la personne n'a pas encore levé l'appareil.
MINIATURE_POSITION = 0.5

#: Les trois instants candidats du VIDEO-2 (décision du 13/09/2026 : la miniature sert
#: d'`og:image` au VIDEO-3, elle se choisit plutôt que de tomber sur un mouvement de
#: bouche au hasard). Espacés pour donner trois images vraiment différentes, sans
#: mordre sur les tout premiers instants — flous de mise au point — ni sur la toute fin,
#: souvent déjà en train de couper.
MINIATURE_POSITIONS_CANDIDATES = (0.25, 0.5, 0.75)

#: Qualité JPEG de la miniature, à l'échelle `-q:v` de ffmpeg (2 = meilleure, 31 = pire).
MINIATURE_QUALITE = 4

#: Au-delà, on abandonne l'encodage. 25 s de vidéo encodées en plus de trois minutes,
#: c'est que quelque chose est parti en vrille — pas que la machine est lente.
DELAI_MAX_SECONDES = 180


@dataclass(frozen=True)
class ProfilVideo:
    """Un jeu de réglages d'encodage, nommé — parce que le nom est écrit en base.

    Le profil est enregistré à côté du chemin du fichier : c'est ce qui permettra, le
    jour où le débit change, de savoir laquelle des vidéos déjà en ligne a été encodée
    avec quoi, sans avoir à sonder le disque entier.
    """

    nom: str
    #: Le CÔTÉ COURT et le CÔTÉ LONG du cadre, pas une largeur et une hauteur : voir
    #: `dimensions_cibles()`, qui les applique selon l'orientation de la source.
    cote_court: int
    cote_long: int
    debit_video_kbps: int
    debit_audio_kbps: int

    @property
    def debit_total_kbps(self) -> int:
        return self.debit_video_kbps + self.debit_audio_kbps

    def __str__(self) -> str:
        return (
            f"{self.nom} ({self.cote_court}x{self.cote_long}, "
            f"{self.debit_total_kbps} kbit/s)"
        )


#: Le profil retenu au cadrage : 480x854 en portrait, ~800 kbit/s son compris. Un seul
#: profil existe — le cadrage a tranché contre la comparaison de deux. Le dictionnaire
#: `PROFILS` n'est pas de l'anticipation gratuite : la colonne `video_profil` en base
#: doit pouvoir être relue en objet, y compris pour un profil qui aurait été retiré.
PROFIL_ECONOMIQUE = ProfilVideo(
    nom="economique",
    cote_court=480,
    cote_long=854,
    debit_video_kbps=700,
    debit_audio_kbps=96,
)

PROFILS: dict[str, ProfilVideo] = {PROFIL_ECONOMIQUE.nom: PROFIL_ECONOMIQUE}
PROFIL_PAR_DEFAUT = PROFIL_ECONOMIQUE


# --------------------------------------------------------------------------------
# Les erreurs — une par cause, parce qu'elles ne se racontent pas pareil
# --------------------------------------------------------------------------------
# Au VIDEO-2, chacune devra devenir une phrase montrée à quelqu'un. « Votre vidéo dure
# 31 s, la limite est 25 s » se corrige ; « le serveur n'a pas pu lire ce fichier » se
# contourne ; « ffmpeg est absent » ne concerne pas la personne du tout et doit réveiller
# un administrateur. Une seule exception fourre-tout les confondrait toutes.


class ErreurVideo(Exception):
    """Racine de tout ce qui peut mal se passer dans ce module."""


class OutilAbsent(ErreurVideo):
    """`ffmpeg` ou `ffprobe` n'est pas installé. Panne de serveur, pas faute d'usager."""


class FichierIllisible(ErreurVideo):
    """Fichier corrompu, format non reconnu, ou aucune piste vidéo dedans."""


class VideoTropLongue(ErreurVideo):
    """La durée MESURÉE dépasse le seuil. Porte la durée, pour pouvoir la dire."""

    def __init__(self, duree: float, seuil: float = SEUIL_REJET_SECONDES) -> None:
        self.duree = duree
        self.seuil = seuil
        super().__init__(
            f"vidéo de {duree:.2f} s : au-delà des {DUREE_MAX_SECONDES:.0f} s autorisées "
            f"(seuil de rejet {seuil:.2f} s, marge d'arrondi comprise)"
        )


class EchecEncodage(ErreurVideo):
    """`ffmpeg` a rendu la main sur une erreur, ou n'a pas rendu la main du tout."""


class FichierTropGros(ErreurVideo):
    """Le fichier REÇU dépasse le plafond — avant tout transcodage. Porte les deux
    nombres, pour pouvoir dire l'un et l'autre."""

    def __init__(self, octets_recus: int, plafond: int) -> None:
        self.octets_recus = octets_recus
        self.plafond = plafond
        super().__init__(
            f"fichier au-delà de {plafond // 1_000_000} Mo (arrêté à "
            f"{octets_recus // 1_000_000} Mo reçus)"
        )


# --------------------------------------------------------------------------------
# Recevoir
# --------------------------------------------------------------------------------
#
# Partagé par l'écran de modération (VIDEO-2) et le formulaire participant (VIDEO-3+) :
# les deux reçoivent un fichier par le réseau et doivent s'arrêter avant le plafond,
# pas après. `lecture` est délibérément un simple `Callable`, pas un type FastAPI —
# ce module ne connaît ni formulaire ni requête HTTP (voir l'en-tête du fichier), et
# `UploadFile.read` s'y prête tel quel, en méthode liée.

#: Lu par blocs plutôt que d'un coup : inutile de charger en mémoire un fichier qui
#: se compte en dizaines de méga-octets avant même de savoir s'il sera accepté.
_MORCEAU_LECTURE = 1024 * 1024


async def recevoir(
    lecture: Callable[[int], Awaitable[bytes]],
    plafond_octets: int,
    suffixe: str = "",
) -> Path:
    """Écrit un flux reçu sur un fichier temporaire, EN COMPTANT.

    Lève `FichierTropGros` dès le morceau qui fait dépasser le plafond — sans avoir
    laissé le fichier temporaire grossir au-delà, et sans l'avoir laissé sur le disque :
    à l'appelant de nettoyer le fichier qu'IL a reçu (le fichier brut d'origine, hors de
    ce module), celui-ci ne gère que sa propre écriture.
    """
    descripteur, brut = tempfile.mkstemp(prefix="video-recue-", suffix=suffixe)
    chemin = Path(brut)
    octets = 0
    with os.fdopen(descripteur, "wb") as sortie:
        while morceau := await lecture(_MORCEAU_LECTURE):
            octets += len(morceau)
            if octets > plafond_octets:
                sortie.close()
                chemin.unlink(missing_ok=True)
                raise FichierTropGros(octets, plafond_octets)
            sortie.write(morceau)
    return chemin


# --------------------------------------------------------------------------------
# Sonder
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Sonde:
    """Ce que `ffprobe` dit d'un fichier — les faits, pas les déclarations du client."""

    duree_s: float
    largeur: int
    hauteur: int
    codec_video: str
    codec_audio: str | None
    debit_kbps: int | None
    images_par_seconde: float | None
    octets: int

    @property
    def est_portrait(self) -> bool:
        return self.hauteur >= self.largeur


async def _executer(programme: str, *arguments: str) -> tuple[int, bytes, bytes]:
    """Lance un outil externe et rend (code, sortie, erreurs).

    Traduit l'absence du binaire en `OutilAbsent` ici, une fois : tous les appelants s'en
    remettent à cette traduction plutôt que de rattraper `FileNotFoundError` chacun de
    leur côté — c'est la seule façon que le message reste le même partout.
    """
    try:
        processus = await asyncio.create_subprocess_exec(
            programme,
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as erreur:
        raise OutilAbsent(
            f"{programme} est introuvable. C'est une dépendance SYSTÈME (paquet "
            f"« ffmpeg »), pas une bibliothèque Python : elle s'installe sur la machine, "
            f"pas par pip."
        ) from erreur

    try:
        sortie, erreurs = await asyncio.wait_for(
            processus.communicate(), timeout=DELAI_MAX_SECONDES
        )
    except asyncio.TimeoutError as erreur:
        processus.kill()
        await processus.wait()
        raise EchecEncodage(
            f"{programme} n'a pas rendu la main en {DELAI_MAX_SECONDES} s — abandonné."
        ) from erreur

    return processus.returncode or 0, sortie, erreurs


def _dernieres_lignes(erreurs: bytes, combien: int = 4) -> str:
    """Les dernières lignes de la sortie d'erreur — ffmpeg dit la cause tout à la fin."""
    lignes = [
        ligne.strip()
        for ligne in erreurs.decode("utf-8", "replace").splitlines()
        if ligne.strip()
    ]
    return " | ".join(lignes[-combien:]) or "(aucun message)"


async def sonder(chemin: Path | str) -> Sonde:
    """Mesure un fichier vidéo. Lève `FichierIllisible` s'il n'y a rien à mesurer."""
    chemin = Path(chemin)
    if not chemin.is_file():
        raise FichierIllisible(f"fichier introuvable : {chemin}")

    code, sortie, erreurs = await _executer(
        "ffprobe",
        "-v", "error",
        "-show_streams",
        "-show_format",
        "-of", "json",
        str(chemin),
    )
    if code != 0:
        raise FichierIllisible(
            f"ffprobe ne sait pas lire ce fichier : {_dernieres_lignes(erreurs)}"
        )

    try:
        donnees = json.loads(sortie or b"{}")
    except json.JSONDecodeError as erreur:  # pragma: no cover - ffprobe rendrait 0 sans JSON
        raise FichierIllisible("ffprobe a rendu une réponse illisible") from erreur

    flux = donnees.get("streams") or []
    video = next((f for f in flux if f.get("codec_type") == "video"), None)
    audio = next((f for f in flux if f.get("codec_type") == "audio"), None)
    if video is None:
        raise FichierIllisible(
            "aucune piste vidéo dans ce fichier (un son, une image ou un fichier "
            "d'un autre type ?)"
        )

    format_ = donnees.get("format") or {}
    duree = _flottant(format_.get("duration")) or _flottant(video.get("duration"))
    if duree is None or duree <= 0:
        # Un conteneur sans durée lisible n'est pas forcément corrompu (flux en direct,
        # écriture interrompue) — mais il est inutilisable ici : c'est la durée qui décide
        # du rejet, et on ne devine pas ce qu'on n'a pas pu lire.
        raise FichierIllisible("durée illisible — fichier tronqué ou conteneur incomplet")

    largeur, hauteur = _dimensions_affichees(video)
    debit = _flottant(format_.get("bit_rate"))

    return Sonde(
        duree_s=duree,
        largeur=largeur,
        hauteur=hauteur,
        codec_video=video.get("codec_name", "?"),
        codec_audio=audio.get("codec_name") if audio else None,
        debit_kbps=int(debit / 1000) if debit else None,
        images_par_seconde=_fraction(video.get("avg_frame_rate")),
        octets=chemin.stat().st_size,
    )


def _flottant(valeur: object) -> float | None:
    try:
        return float(valeur)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _fraction(valeur: object) -> float | None:
    """« 30000/1001 » -> 29.97. ffprobe rend les cadences en fraction exacte."""
    if not isinstance(valeur, str) or "/" not in valeur:
        return _flottant(valeur)
    haut, _, bas = valeur.partition("/")
    numerateur, denominateur = _flottant(haut), _flottant(bas)
    if not numerateur or not denominateur:
        return None
    return numerateur / denominateur


def _dimensions_affichees(video: dict) -> tuple[int, int]:
    """Les dimensions telles qu'un lecteur les MONTRE, rotation comprise.

    Un iPhone tenu à la verticale n'écrit pas une image verticale : il écrit une image
    1920x1080 accompagnée d'une matrice de rotation de 90°, et c'est le lecteur qui
    redresse. Lire `width`/`height` sans regarder la rotation ferait donc traiter comme
    paysage la moitié des vidéos de téléphone — et leur donnerait un cadre couché.
    """
    largeur = int(video.get("width") or 0)
    hauteur = int(video.get("height") or 0)

    rotation = 0.0
    for annexe in video.get("side_data_list") or []:
        if "rotation" in annexe:
            rotation = _flottant(annexe["rotation"]) or 0.0
    # `tags.rotate` est la forme ancienne, encore produite par bien des fichiers.
    if not rotation:
        rotation = _flottant((video.get("tags") or {}).get("rotate")) or 0.0

    if int(abs(rotation)) % 180 == 90:
        largeur, hauteur = hauteur, largeur
    return largeur, hauteur


# --------------------------------------------------------------------------------
# Décider du cadre
# --------------------------------------------------------------------------------


def dimensions_cibles(
    largeur: int, hauteur: int, profil: ProfilVideo = PROFIL_PAR_DEFAUT
) -> tuple[int, int]:
    """Le cadre de sortie : le profil est une BORNE, pas une taille imposée.

    Le profil dit 480x854 en portrait. Appliqué tel quel à une vidéo filmée en paysage,
    il produirait une image de 480x270 noyée dans deux bandes noires occupant les deux
    tiers du fichier — des bandes qu'il faudrait ensuite encoder, stocker et servir, et
    qu'aucun lecteur ne saurait retirer. On garde donc la forme de la source et on la
    fait tenir dans le budget de pixels du profil : portrait -> 480x854, paysage ->
    854x480, carré -> 480x480. Le lecteur, lui, sait mettre des bandes noires autour, et
    elles ne coûtent alors pas un octet.

    **On ne sur-échantillonne jamais.** Une vidéo filmée en 320x240 sort en 320x240 :
    l'agrandir n'ajoute aucun détail, coûte du débit, et rend le flou plus visible.
    """
    if largeur <= 0 or hauteur <= 0:
        raise FichierIllisible(f"dimensions absurdes : {largeur}x{hauteur}")

    if hauteur >= largeur:
        cadre_l, cadre_h = profil.cote_court, profil.cote_long
    else:
        cadre_l, cadre_h = profil.cote_long, profil.cote_court

    facteur = min(cadre_l / largeur, cadre_h / hauteur, 1.0)
    return _pair(largeur * facteur), _pair(hauteur * facteur)


def _pair(valeur: float) -> int:
    """Arrondit à l'entier pair le plus proche, au moins 2.

    H.264 en 4:2:0 échantillonne la chrominance une fois pour deux pixels : une dimension
    impaire est refusée par l'encodeur. Ce n'est pas de la coquetterie d'alignement.
    """
    return max(2, int(round(valeur / 2)) * 2)


# --------------------------------------------------------------------------------
# Le pipeline
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class ResultatTranscodage:
    """Ce que le pipeline rend — des chemins et des mesures, jamais d'octets vidéo."""

    chemin_video: str
    """Chemin RELATIF à `settings.video_racine`. Relatif, pour que déplacer le répertoire
    des médias (ou monter un autre disque sur le VPS) ne demande pas de migration."""

    chemin_miniature: str
    duree_s: float
    profil: str

    octets_source: int
    octets_video: int
    octets_miniature: int
    secondes_encodage: float

    largeur: int
    hauteur: int
    debit_effectif_kbps: int | None
    sonde_source: Sonde

    @property
    def taux_compression(self) -> float:
        """Combien de fois le fichier a rétréci. 12.0 = douze fois plus petit."""
        return self.octets_source / self.octets_video if self.octets_video else 0.0


def racine_media() -> Path:
    """Le répertoire où vivent les fichiers. Créé s'il manque."""
    racine = Path(settings.video_racine).expanduser()
    racine.mkdir(parents=True, exist_ok=True)
    return racine


async def verifier_duree(chemin: Path | str) -> Sonde:
    """Sonde le fichier et refuse tout ce qui dépasse le seuil.

    Séparée de `transcoder()` à dessein : c'est le contrôle qui doit avoir lieu AVANT
    qu'une seconde de calcul soit dépensée sur un fichier qu'on va refuser.
    """
    sonde = await sonder(chemin)
    if sonde.duree_s > SEUIL_REJET_SECONDES:
        raise VideoTropLongue(sonde.duree_s)
    return sonde


async def transcoder(
    source: Path | str,
    destination: Path | str,
    sonde: Sonde,
    profil: ProfilVideo = PROFIL_PAR_DEFAUT,
) -> None:
    """H.264 + AAC dans un `.mp4`, au cadre et au débit du profil, `faststart` compris."""
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    largeur, hauteur = dimensions_cibles(sonde.largeur, sonde.hauteur, profil)

    cadence = min(
        IMAGES_PAR_SECONDE_MAX, math.ceil(sonde.images_par_seconde or IMAGES_PAR_SECONDE_MAX)
    )

    arguments = [
        "-nostdin",
        "-y",
        "-i", str(source),
        # Dernier garde-fou de durée. La sonde a déjà refusé au-delà du seuil ; ce
        # `-t` garantit qu'un conteneur qui aurait menti sur sa durée ne produit pas
        # pour autant un fichier de dix minutes sur le disque.
        "-t", f"{SEUIL_REJET_SECONDES:.3f}",
        "-vf", f"scale={largeur}:{hauteur},setsar=1",
        "-r", str(cadence),
        "-c:v", "libx264",
        # `main` et non `high` : c'est le profil que lisent aussi les appareils anciens,
        # et à 700 kbit/s sur 480x854 le gain de `high` ne se voit pas.
        "-profile:v", "main",
        "-preset", "medium",
        "-b:v", f"{profil.debit_video_kbps}k",
        # Le plafond instantané borne ce qu'un plan agité peut consommer ; sans lui, le
        # débit moyen est tenu mais un réseau mobile décroche sur les pics.
        "-maxrate", f"{int(profil.debit_video_kbps * 1.3)}k",
        "-bufsize", f"{profil.debit_video_kbps * 2}k",
        # Sans `yuv420p`, une source en 4:2:2 ou 10 bits (certains téléphones) produit un
        # fichier que Safari et les téléviseurs refusent d'afficher.
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", f"{profil.debit_audio_kbps}k",
        "-ac", "2",
        "-ar", "44100",
        # Le drapeau demandé : l'atome `moov` remonte en tête du fichier. Sans lui, un
        # lecteur doit avoir téléchargé la fin avant de montrer le début, et l'aperçu de
        # partage du VIDEO-3 ne saurait rien afficher du tout.
        "-movflags", "+faststart",
        str(destination),
    ]

    code, _, erreurs = await _executer("ffmpeg", *arguments)
    if code != 0:
        raise EchecEncodage(f"ffmpeg a échoué : {_dernieres_lignes(erreurs)}")
    if not destination.is_file() or destination.stat().st_size == 0:
        raise EchecEncodage("ffmpeg a rendu 0 mais n'a écrit aucun fichier")


async def extraire_miniature(
    video: Path | str,
    destination: Path | str,
    duree_s: float,
    position: float = MINIATURE_POSITION,
) -> None:
    """Une image JPEG prise à `position` (part de la durée, milieu par défaut).

    Extraite du fichier TRANSCODÉ, pas de la source : c'est l'image qu'on montrera à côté
    de la vidéo, elle doit donc avoir son cadrage et ses couleurs — et sur un `.mp4` en
    faststart, le positionnement est immédiat.
    """
    video, destination = Path(video), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    instant = max(0.0, duree_s * position)

    code, _, erreurs = await _executer(
        "ffmpeg",
        "-nostdin",
        "-y",
        "-ss", f"{instant:.3f}",
        "-i", str(video),
        "-frames:v", "1",
        "-q:v", str(MINIATURE_QUALITE),
        str(destination),
    )
    if code != 0 or not destination.is_file() or destination.stat().st_size == 0:
        raise EchecEncodage(
            f"miniature non produite : {_dernieres_lignes(erreurs)}"
        )


async def preparer(
    source: Path | str,
    profil: ProfilVideo = PROFIL_PAR_DEFAUT,
    sous_dossier: str = "",
    racine: Path | None = None,
) -> ResultatTranscodage:
    """Le pipeline complet : vérifier, transcoder, extraire, mesurer.

    Écrit sous `racine / sous_dossier` et rend des chemins RELATIFS à la racine. Rien
    n'est écrit avant que la durée soit vérifiée ; rien n'est laissé derrière si une
    étape échoue — un `.mp4` orphelin sans sa miniature serait servi un jour par erreur.
    """
    source = Path(source)
    racine = racine or racine_media()
    sonde = await verifier_duree(source)

    # Un nom tiré au hasard, jamais celui du fichier reçu. Le nom d'origine vient du
    # client : il peut contenir n'importe quoi, y compris des séparateurs de chemin, et
    # il n'apporte rien puisque le fichier est retrouvé par la base.
    base = uuid.uuid4().hex
    dossier = racine / sous_dossier if sous_dossier else racine
    fichier_video = dossier / f"{base}.mp4"
    fichier_miniature = dossier / f"{base}.jpg"

    depart = time.perf_counter()
    try:
        await transcoder(source, fichier_video, sonde, profil)
        await extraire_miniature(fichier_video, fichier_miniature, sonde.duree_s)
    except ErreurVideo:
        fichier_video.unlink(missing_ok=True)
        fichier_miniature.unlink(missing_ok=True)
        raise
    secondes = time.perf_counter() - depart

    produite = await sonder(fichier_video)
    relatif_video = str(fichier_video.relative_to(racine))
    relatif_miniature = str(fichier_miniature.relative_to(racine))
    logger.info(
        "vidéo transcodée : %s, %.2f s, %d -> %d octets en %.1f s (profil %s)",
        relatif_video, sonde.duree_s, sonde.octets, produite.octets, secondes, profil.nom,
    )

    return ResultatTranscodage(
        chemin_video=relatif_video,
        chemin_miniature=relatif_miniature,
        duree_s=round(sonde.duree_s, 3),
        profil=profil.nom,
        octets_source=sonde.octets,
        octets_video=produite.octets,
        octets_miniature=fichier_miniature.stat().st_size,
        secondes_encodage=secondes,
        largeur=produite.largeur,
        hauteur=produite.hauteur,
        debit_effectif_kbps=produite.debit_kbps,
        sonde_source=sonde,
    )


@dataclass(frozen=True)
class ResultatTranscodageChoix:
    """Comme `ResultatTranscodage`, mais SANS miniature retenue : trois candidates
    attendent d'être départagées par `choisir_miniature()` (VIDEO-2)."""

    chemin_video: str
    duree_s: float
    profil: str

    octets_source: int
    octets_video: int
    secondes_encodage: float

    largeur: int
    hauteur: int
    debit_effectif_kbps: int | None
    sonde_source: Sonde


async def extraire_miniatures_candidates(
    video: Path | str,
    dossier: Path | str,
    duree_s: float,
    base: str,
    positions: tuple[float, ...] = MINIATURE_POSITIONS_CANDIDATES,
) -> list[Path]:
    """Trois miniatures au lieu d'une, nommées `{base}-choix-0.jpg` etc.

    Le nom porte le rang plutôt qu'un identifiant tiré à part : `choisir_miniature()`
    n'a ainsi besoin d'aucun état supplémentaire pour retrouver les candidates — juste
    le chemin de la vidéo, déjà en base.
    """
    dossier = Path(dossier)
    chemins: list[Path] = []
    try:
        for indice, position in enumerate(positions):
            destination = dossier / f"{base}-choix-{indice}.jpg"
            await extraire_miniature(video, destination, duree_s, position=position)
            chemins.append(destination)
    except ErreurVideo:
        for chemin in chemins:
            chemin.unlink(missing_ok=True)
        raise
    return chemins


async def preparer_avec_choix_miniature(
    source: Path | str,
    profil: ProfilVideo = PROFIL_PAR_DEFAUT,
    sous_dossier: str = "",
    racine: Path | None = None,
) -> ResultatTranscodageChoix:
    """Le pipeline du VIDEO-2 : vérifier, transcoder, puis extraire TROIS miniatures
    candidates plutôt qu'une (décision du 13/09/2026 — la miniature sert d'`og:image`
    au VIDEO-3, ce n'est plus une image prise au hasard d'un mouvement de bouche).

    Séparée de `preparer()` plutôt que de lui ajouter un drapeau : `ResultatTranscodage`
    et son unique `chemin_miniature` restent inchangés pour `video-joindre` et pour les
    18 cas de VIDEO-1, qui n'ont rien à choisir.
    """
    source = Path(source)
    racine = racine or racine_media()
    sonde = await verifier_duree(source)

    base = uuid.uuid4().hex
    dossier = racine / sous_dossier if sous_dossier else racine
    fichier_video = dossier / f"{base}.mp4"

    depart = time.perf_counter()
    try:
        await transcoder(source, fichier_video, sonde, profil)
        await extraire_miniatures_candidates(fichier_video, dossier, sonde.duree_s, base)
    except ErreurVideo:
        fichier_video.unlink(missing_ok=True)
        for indice in range(len(MINIATURE_POSITIONS_CANDIDATES)):
            (dossier / f"{base}-choix-{indice}.jpg").unlink(missing_ok=True)
        raise
    secondes = time.perf_counter() - depart

    produite = await sonder(fichier_video)
    relatif_video = str(fichier_video.relative_to(racine))
    logger.info(
        "vidéo transcodée (choix de miniature) : %s, %.2f s, %d -> %d octets en %.1f s "
        "(profil %s)",
        relatif_video, sonde.duree_s, sonde.octets, produite.octets, secondes, profil.nom,
    )

    return ResultatTranscodageChoix(
        chemin_video=relatif_video,
        duree_s=round(sonde.duree_s, 3),
        profil=profil.nom,
        octets_source=sonde.octets,
        octets_video=produite.octets,
        secondes_encodage=secondes,
        largeur=produite.largeur,
        hauteur=produite.hauteur,
        debit_effectif_kbps=produite.debit_kbps,
        sonde_source=sonde,
    )


def choisir_miniature(
    chemin_video_relatif: str, indice: int, racine: Path | None = None
) -> str:
    """Retient la candidate `indice` comme couverture définitive, efface les deux
    autres. Rend son chemin relatif — à écrire dans `conversation.video_miniature_chemin`.
    """
    racine = (racine or racine_media()).resolve()
    video = (racine / chemin_video_relatif).resolve()
    if racine not in video.parents:
        raise ErreurVideo(f"chemin hors du répertoire des médias : {chemin_video_relatif}")
    if not 0 <= indice < len(MINIATURE_POSITIONS_CANDIDATES):
        raise ErreurVideo(f"indice de miniature hors bornes : {indice}")

    base, dossier = video.stem, video.parent
    choisie = dossier / f"{base}-choix-{indice}.jpg"
    if not choisie.is_file():
        raise ErreurVideo(f"miniature candidate introuvable : {choisie.name}")

    finale = dossier / f"{base}.jpg"
    choisie.rename(finale)
    for autre in range(len(MINIATURE_POSITIONS_CANDIDATES)):
        if autre != indice:
            (dossier / f"{base}-choix-{autre}.jpg").unlink(missing_ok=True)
    return str(finale.relative_to(racine))


def supprimer_candidates_miniatures(
    chemin_video_relatif: str, racine: Path | None = None
) -> None:
    """Efface les candidates encore en attente de choix — appelé quand une vidéo est
    remplacée ou retirée avant que quiconque ait tranché."""
    racine = (racine or racine_media()).resolve()
    video = (racine / chemin_video_relatif).resolve()
    if racine not in video.parents:
        raise ErreurVideo(f"chemin hors du répertoire des médias : {chemin_video_relatif}")
    base, dossier = video.stem, video.parent
    for indice in range(len(MINIATURE_POSITIONS_CANDIDATES)):
        (dossier / f"{base}-choix-{indice}.jpg").unlink(missing_ok=True)


def supprimer(chemin_relatif: str | None, racine: Path | None = None) -> None:
    """Efface un fichier désigné par son chemin relatif, sans jamais sortir de la racine.

    Le chemin vient de la base, donc en principe de nous — mais « en principe » ne
    suffit pas pour une fonction qui efface : un `..` glissé dans la colonne ferait
    supprimer un fichier arbitraire du VPS.
    """
    if not chemin_relatif:
        return
    racine = (racine or racine_media()).resolve()
    cible = (racine / chemin_relatif).resolve()
    if racine not in cible.parents:
        raise ErreurVideo(f"chemin hors du répertoire des médias : {chemin_relatif}")
    cible.unlink(missing_ok=True)


def outils_disponibles() -> bool:
    """`ffmpeg` et `ffprobe` sont-ils installés ? Sert aux tests et au banc de mesure."""
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
