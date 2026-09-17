"""Fabrique le corpus du banc-vidéo : de vraies prises de vue, aux réglages d'un téléphone.

    python video_mesures/preparer_corpus.py [--dest media/banc-corpus]
    python -m app.cli banc-video --dossier media/banc-corpus/corpus

**Pourquoi ce script existe.** Le banc doit mesurer ce que coûte une vidéo de 25 s
envoyée par quelqu'un depuis son téléphone. Il n'y a pas de téléphone sur cette machine,
et une mire `testsrc` de ffmpeg ne mesurerait rien de réel : une image de synthèse sans
grain ni tremblement se compresse trois à quatre fois mieux qu'un visage filmé à la main
dans un salon, et donnerait un poids flatteur et faux.

Le corpus est donc fait en deux temps, et les deux sont honnêtes séparément :

  1. **Le contenu est réel** — prises de vue de Wikimedia Commons, librement licenciées,
     choisies pour ressembler à l'usage visé : des gens qui parlent face caméra, des
     plans à la main, du portrait. Provenance et licence : voir README.md, à côté.
  2. **Le contenant est refait aux réglages d'un téléphone** — Commons ne distribue que
     du WebM/VP9 ré-encodé à ~2 Mbit/s, là où un téléphone rend du H.264 ou du HEVC à
     8-20 Mbit/s. Sans cette étape, le « taux de compression » mesuré par le banc
     comparerait notre sortie à un fichier déjà compressé, donc ne voudrait rien dire.

Ce que ce corpus NE reproduit pas, et qu'il faut garder en tête en lisant le banc : il
ne contient pas de 4K (les sources sont en 1080p, et agrandir fabriquerait une image
artificiellement facile à encoder), et le contenu a déjà subi une compression avec pertes
chez Commons — le nôtre travaille donc sur une image très légèrement plus lisse qu'un
fichier sorti d'un capteur.
"""

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

UA = "banc-video-rond-point/1.0"
API = "https://commons.wikimedia.org/w/api.php"

#: Durée des extraits. Un poil sous les 25 s réglementaires, comme le rendrait un
#: téléphone dont l'usager a relâché le bouton « à peu près » à temps.
DUREE_EXTRAIT = 24.8


@dataclass(frozen=True)
class Reglage:
    """Les réglages d'enregistrement d'une famille de téléphones."""

    nom: str
    conteneur: str
    codec: str
    debit_kbps: int
    images_par_seconde: int
    debit_audio_kbps: int
    #: Écrit l'image couchée et une matrice de rotation de 90°, comme un iPhone tenu à
    #: la verticale. C'est le piège que le pipeline doit passer : lire `width`/`height`
    #: sans regarder la rotation donnerait un cadre de sortie couché.
    rotation_metadonnee: bool = False


ANDROID_1080 = Reglage("android-1080p", "mp4", "libx264", 17_000, 30, 192)
ANDROID_1080_60 = Reglage("android-1080p60", "mp4", "libx264", 20_000, 60, 192)
IPHONE_HEVC = Reglage("iphone-hevc", "mov", "libx265", 14_000, 30, 128)
IPHONE_ROTATION = Reglage("iphone-rotation", "mov", "libx264", 16_000, 30, 128, True)
ENTREE_DE_GAMME = Reglage("entree-de-gamme", "mp4", "libx264", 6_000, 30, 96)

#: Chaque entrée : nom local, titre exact sur Commons, réglage d'enregistrement, et
#: seconde à partir de laquelle prendre l'extrait (choisie à l'œil, pour tomber sur du
#: contenu et pas sur un carton de titre ou une mise au point).
CORPUS: list[tuple[str, str, Reglage, float, float]] = [
    (
        "selfie-biden-uaw",
        "File:President Biden records a selfie video with Shawn Fain of the UAW.webm",
        IPHONE_HEVC, 4.0, DUREE_EXTRAIT,
    ),
    (
        "marche-riviere-japon",
        "File:Walking along a rural riverside path in Japan June2025.webm",
        ANDROID_1080, 3.0, DUREE_EXTRAIT,
    ),
    (
        "portrait-wikipedia-25",
        "File:WP25Gabe.webm",
        IPHONE_ROTATION, 10.0, DUREE_EXTRAIT,
    ),
    (
        "principes-wikipedia",
        "File:Les principes fondateurs de Wikipédia.webm",
        ANDROID_1080, 20.0, DUREE_EXTRAIT,
    ),
    (
        "vlog-valentijn",
        "File:Vlog -2 - valentijn.webm",
        ENTREE_DE_GAMME, 30.0, DUREE_EXTRAIT,
    ),
    (
        "chute-mbimbi",
        "File:Chute de la Rivière Mbimbi, Ville de MATARI, Feshi, Kwango, République Démocratique du Congo.webm",
        ENTREE_DE_GAMME, 8.0, DUREE_EXTRAIT,
    ),
    (
        "selfie-visage-60ips",
        "File:Her face is clickable in selfie mode.webm",
        ANDROID_1080_60, 6.0, DUREE_EXTRAIT,
    ),
    (
        "ours-zoo-mouvement",
        "File:Bear walking at Kaliningrad Zoo.webm",
        IPHONE_HEVC, 5.0, DUREE_EXTRAIT,
    ),
    # Volontairement trop long : c'est le fichier qui doit se faire REFUSER par le
    # pipeline. Un banc qui ne montre que des réussites ne prouve pas que le contrôle
    # de durée s'applique aussi à de vrais fichiers.
    (
        "trop-long-27s",
        "File:Les principes fondateurs de Wikipédia.webm",
        ANDROID_1080, 60.0, 27.0,
    ),
]


def curl(*arguments: str) -> bytes:
    return subprocess.run(
        ["curl", "-sL", "-A", UA, "--max-time", "600", *arguments],
        capture_output=True,
    ).stdout


def urls_commons(titres: list[str]) -> dict[str, str]:
    """Résout des titres Commons en URLs de fichier. Une seule requête pour tous."""
    brut = curl(
        API, "-G",
        "--data-urlencode", "action=query",
        "--data-urlencode", "prop=imageinfo",
        "--data-urlencode", "iiprop=url",
        "--data-urlencode", "format=json",
        "--data-urlencode", "titles=" + "|".join(dict.fromkeys(titres)),
    )
    pages = (json.loads(brut or b"{}").get("query") or {}).get("pages") or {}
    resolues = {}
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        if info.get("url"):
            # Commons ajoute des paramètres de suivi à l'URL ; la partie utile s'arrête
            # au « ? », et l'extension aussi.
            resolues[page["title"]] = info["url"].split("?", 1)[0]
    return resolues


def telecharger(url: str, destination: Path) -> None:
    if destination.exists() and destination.stat().st_size > 0:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    curl(url, "-o", str(destination))


def extraire(source: Path, destination: Path, reglage: Reglage, debut: float, duree: float) -> None:
    """Coupe un extrait et le ré-encode aux réglages d'enregistrement d'un téléphone."""
    if destination.exists() and destination.stat().st_size > 0:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)

    filtres = []
    if reglage.rotation_metadonnee:
        # On couche l'image (l'inverse de ce qu'un lecteur fera) et on écrit la rotation
        # à côté : c'est exactement la forme d'un fichier d'iPhone filmé à la verticale.
        filtres.append("transpose=1")
    filtres.append("setsar=1")

    arguments = [
        "ffmpeg", "-nostdin", "-y",
        "-ss", f"{debut:.3f}",
        "-i", str(source),
        "-t", f"{duree:.3f}",
        "-vf", ",".join(filtres),
        "-r", str(reglage.images_par_seconde),
        "-c:v", reglage.codec,
        "-b:v", f"{reglage.debit_kbps}k",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", f"{reglage.debit_audio_kbps}k",
        "-ar", "48000",
    ]
    if reglage.codec == "libx265":
        # Sans cette étiquette, un HEVC en .mov n'est lu ni par QuickTime ni par Safari
        # — et l'intérêt de cet échantillon est justement d'être un fichier d'iPhone.
        arguments += ["-tag:v", "hvc1"]
    arguments.append(str(destination) if not reglage.rotation_metadonnee else str(destination) + ".couche.mov")

    resultat = subprocess.run(arguments, capture_output=True, text=True)
    if resultat.returncode != 0:
        derniere = [l for l in resultat.stderr.splitlines() if l.strip()][-3:]
        raise SystemExit(f"ffmpeg a échoué sur {destination.name} :\n  " + "\n  ".join(derniere))

    if reglage.rotation_metadonnee:
        # La matrice de rotation s'écrit en second passage, par remultiplexage sans
        # ré-encodage. `-metadata:s:v rotate=90` ne fonctionne plus depuis ffmpeg 6 : la
        # forme retenue est `-display_rotation`, option d'ENTRÉE, dont la valeur est
        # recopiée dans le fichier de sortie quand les flux sont copiés tels quels.
        couche = Path(str(destination) + ".couche.mov")
        remux = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-v", "error",
             "-display_rotation", "90", "-i", str(couche),
             "-map", "0", "-c", "copy", str(destination)],
            capture_output=True, text=True,
        )
        couche.unlink(missing_ok=True)
        if remux.returncode != 0:
            raise SystemExit(f"remultiplexage échoué sur {destination.name} :\n{remux.stderr}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", default="media/banc-corpus")
    arguments = parser.parse_args()

    dest = Path(arguments.dest).expanduser()
    originaux, corpus = dest / "originaux", dest / "corpus"

    urls = urls_commons([titre for _, titre, _, _, _ in CORPUS])
    manquants = {t for _, t, _, _, _ in CORPUS} - set(urls)
    if manquants:
        print("Introuvables sur Commons :", *sorted(manquants), sep="\n  ", file=sys.stderr)
        return 1

    for nom, titre, reglage, debut, duree in CORPUS:
        url = urls[titre]
        original = originaux / (titre[5:].replace(" ", "_")[:80] + Path(url).suffix)
        print(f"  téléchargement  {original.name[:60]}")
        telecharger(url, original)
        cible = corpus / f"{nom}.{reglage.conteneur}"
        print(f"  extrait         {cible.name}  ({reglage.nom}, {duree:.1f} s)")
        extraire(original, cible, reglage, debut, duree)

    total = sum(f.stat().st_size for f in corpus.iterdir())
    print(f"\n{len(list(corpus.iterdir()))} fichiers dans {corpus} ({total / 1e6:.0f} Mo)")
    print(f"Banc : python -m app.cli banc-video --dossier {corpus}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
