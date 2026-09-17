"""Banc du E2 : deux modèles 7B quantifiés, sur cinq débats, mesurés sur trois axes.

    python3 e2_mesures/banc_e2.py --modele mistral --fils 6

Ce banc ne fait partie ni de l'application ni de `pytest` : il tourne HORS PRODUCTION,
avec un binaire llama.cpp autonome, et n'écrit que dans son dossier de résultats. Même
statut que les bancs de `d0_mesures/`.

## Les trois axes, dans l'ordre où ils décident

1. **La justesse** — un nom qui inverse la position d'un groupe est pire qu'un nom plat.
   C'est le seul axe où l'échec est grave, et le seul que les débats fabriqués rendent
   mesurable : eux seuls portent leur vérité de terrain.
2. **Le biais** — chaque débat est joué DEUX FOIS, groupes présentés dans un ordre puis
   dans l'autre. Si les noms changent selon la place du groupe dans l'invite, le modèle
   ne nomme pas, il récite. C'est la version mesurable, sur nos données, de ce que le
   Computational Democracy Project a trouvé chez Pol.is — des résumés statistiquement
   plus proches d'un groupe que de l'autre. Le risque principal du chantier E, et celui
   qui ne se voit pas si on ne le cherche pas.
3. **Le débit** — le chiffre le plus incertain sur CETTE machine : le banc de 20-28
   tokens/s cité au 5 septembre venait d'un double EPYC, or ce serveur est un Haswell
   virtualisé sans AVX-512. Mesuré, pas supposé.
"""

import argparse
import concurrent.futures
import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from corpus import DEBATS  # noqa: E402
from invite import SYSTEME, invite, json_pur, lit_reponse  # noqa: E402

RACINE = pathlib.Path("/home/ubuntu/chantier-e2")
BINAIRE = RACINE / "bin" / "llama-b10825" / "llama-server"
MODELES = {
    "mistral": RACINE / "modeles" / "mistral-7b-instruct-v0.3-Q4_K_M.gguf",
    "qwen": RACINE / "modeles" / "qwen2.5-7b-instruct-Q4_K_M.gguf",
}
PORT = 8081
BASE = f"http://127.0.0.1:{PORT}"

#: Ce que le chiffrage du 5 septembre a supposé par groupe. Sert de repère au débit.
SORTIE_ATTENDUE_PAR_GROUPE = 30


class Serveur:
    """`llama-server` lancé pour la durée du banc, et arrêté quoi qu'il arrive.

    **Pourquoi le serveur et non `llama-cli`.** Trois raisons, et la troisième est la
    seule qui compte vraiment. Le binaire en ligne de commande mêle sa bannière à la
    sortie du modèle, ce qui rend la lecture fragile ; il recharge 4,4 Go à chaque appel,
    ce qui noierait la mesure de débit sous le temps de chargement ; et surtout **il ne
    sait pas traiter plusieurs requêtes à la fois**, alors que le E2 doit précisément
    éprouver « une charge réaliste, plusieurs débats en parallèle et pas un appel
    isolé ». Mesurer un appel isolé aurait répondu à côté de la question posée.

    Le serveur écoute sur 127.0.0.1 seulement : rien n'est exposé, et le pare-feu du
    chantier C n'a pas à être touché.
    """

    def __init__(self, modele: pathlib.Path, fils: int, creneaux: int):
        self.modele, self.fils, self.creneaux = modele, fils, creneaux
        self.proc = None

    def __enter__(self):
        self.proc = subprocess.Popen(
            [
                str(BINAIRE),
                "-m", str(self.modele),
                "--host", "127.0.0.1",
                "--port", str(PORT),
                "-t", str(self.fils),
                "-c", str(4096 * self.creneaux),
                "-np", str(self.creneaux),
                "--no-warmup",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"LD_LIBRARY_PATH": str(BINAIRE.parent), "PATH": "/usr/bin:/bin"},
        )
        for _ in range(600):
            try:
                with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
                    if r.status == 200:
                        return self
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            if self.proc.poll() is not None:
                raise SystemExit("llama-server s'est arrêté au démarrage")
            time.sleep(1)
        raise SystemExit("llama-server n'a pas répondu en 10 minutes")

    def __exit__(self, *_):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def appelle(message: str, jetons: int = 400) -> dict:
    """Un appel au serveur, et ce qu'il a coûté.

    Température basse et graine fixe : deux exécutions sur la même entrée doivent donner
    la même sortie, sans quoi comparer deux modèles reviendrait à comparer deux tirages.
    La graine ne rend pas le modèle déterministe d'une version à l'autre, elle rend le
    BANC rejouable.
    """
    charge = json.dumps(
        {
            "messages": [
                {"role": "system", "content": SYSTEME},
                {"role": "user", "content": message},
            ],
            "max_tokens": jetons,
            "temperature": 0.2,
            "seed": 42,
        }
    ).encode()
    requete = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=charge,
        headers={"Content-Type": "application/json"},
    )
    debut = time.monotonic()
    with urllib.request.urlopen(requete, timeout=900) as reponse:
        corps = json.loads(reponse.read())
    duree = time.monotonic() - debut

    sortie = corps["choices"][0]["message"]["content"].strip()
    minutage = corps.get("timings") or {}
    return {
        "sortie": sortie,
        "secondes": round(duree, 2),
        "jetons": minutage.get("predicted_n"),
        "debit": (
            round(minutage["predicted_per_second"], 2)
            if minutage.get("predicted_per_second")
            else None
        ),
        "json_pur": json_pur(sortie),
    }


def joue(modele_cle: str, fils: int) -> dict:
    """Les cinq débats, chacun dans les deux ordres, un appel à la fois."""
    resultats = []
    for debat in DEBATS:
        ids = [g["id"] for g in debat["groupes"]]
        # Deux passes : l'ordre naturel, puis l'ordre inverse. C'est le test de biais.
        for etiquette, ordre in (("direct", ids), ("inverse", list(reversed(ids)))):
            message = invite(debat, ordre=ordre)
            print(f"  {debat['cle']:<12} {etiquette:<8} …", end="", flush=True)
            essai = appelle(message)
            noms = lit_reponse(essai["sortie"])
            print(
                f" {essai['secondes']:>6.1f} s"
                f"  {essai['debit'] or 0:>5.1f} tok/s"
                f"  {len(noms)}/{len(ids)} noms"
            )
            resultats.append(
                {
                    "debat": debat["cle"],
                    "reel": debat["reel"],
                    "ordre": etiquette,
                    "n_groupes": len(ids),
                    "noms": {
                        str(g["id"]): {
                            "position_attendue": g["position"],
                            "reference": g["reference"],
                            **noms.get(g["id"], {"nom": "", "justification": ""}),
                        }
                        for g in debat["groupes"]
                    },
                    **{c: essai[c] for c in ("secondes", "jetons", "debit", "json_pur")},
                    "brut": essai["sortie"],
                }
            )
    return resultats


def charge_parallele(creneaux: int) -> dict:
    """Plusieurs débats en même temps — la mesure que le E2 exige vraiment.

    Le chiffrage du 5 septembre raisonne sur un DÉBIT AGRÉGÉ : ce qui compte n'est pas
    la vitesse d'un appel seul mais ce que la machine sort quand plusieurs débats
    tombent ensemble. Les deux chiffres n'ont aucune raison d'être égaux — le traitement
    par lots partage le coût de lecture des poids du modèle, donc l'agrégé monte quand
    le débit d'un appel isolé descend. Mesurer l'un et supposer l'autre était l'erreur
    que ce banc existe pour éviter.
    """
    debats = [d for d in DEBATS for _ in range(2)][:creneaux]
    messages = [invite(d) for d in debats]
    debut = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=creneaux) as pool:
        essais = list(pool.map(appelle, messages))
    duree = time.monotonic() - debut

    jetons = sum(e["jetons"] or 0 for e in essais)
    return {
        "creneaux": creneaux,
        "appels": len(essais),
        "secondes": round(duree, 2),
        "jetons_produits": jetons,
        "debit_agrege": round(jetons / duree, 2) if duree else None,
        "debit_par_appel": [e["debit"] for e in essais],
        "complets": sum(1 for e in essais if lit_reponse(e["sortie"])),
    }


def resume(rapport: dict) -> None:
    essais = rapport["essais"]
    debits = [e["debit"] for e in essais if e["debit"]]
    complets = sum(1 for e in essais if all(n["nom"] for n in e["noms"].values()))
    purs = sum(1 for e in essais if e["json_pur"])

    print(f"\n=== {rapport['modele']} ({rapport['fils']} fils) ===")
    print(f"  essais séquentiels       : {len(essais)}")
    print(f"  réponses complètes       : {complets}/{len(essais)}")
    print(f"  JSON nu, sans enrobage   : {purs}/{len(essais)}")
    if debits:
        median = sorted(debits)[len(debits) // 2]
        print(f"  débit médian (1 appel)   : {median:.1f} tok/s")
        print(f"  débit min / max          : {min(debits):.1f} / {max(debits):.1f} tok/s")
    secondes = [e["secondes"] for e in essais]
    print(f"  durée médiane d'un appel : {sorted(secondes)[len(secondes)//2]:.1f} s")

    for charge in rapport.get("charges", []):
        print(
            f"  {charge['creneaux']} appels en parallèle    : "
            f"{charge['debit_agrege']:.1f} tok/s agrégés "
            f"en {charge['secondes']:.0f} s ({charge['complets']}/{charge['appels']} lus)"
        )

    besoin = 100 * 4 * SORTIE_ATTENDUE_PAR_GROUPE / 600
    meilleur = max(
        [c["debit_agrege"] for c in rapport.get("charges", []) if c["debit_agrege"]]
        + ([sorted(debits)[len(debits) // 2]] if debits else [0])
    )
    print("\n  Repère — 100 débats, 4 groupes, fenêtre de 10 min, TOUS renommés :")
    print(f"    débit agrégé nécessaire  : {besoin:.0f} tok/s")
    print(f"    meilleur agrégé mesuré   : {meilleur:.1f} tok/s")
    if meilleur:
        print(f"    il en manque un facteur  : {besoin / meilleur:.1f}×")


def main() -> int:
    parser = argparse.ArgumentParser(prog="banc_e2")
    parser.add_argument("--modele", choices=sorted(MODELES), required=True)
    parser.add_argument("--fils", type=int, default=6)
    parser.add_argument(
        "--creneaux", type=int, default=4, help="requêtes servies en parallèle"
    )
    parser.add_argument(
        "--charges",
        default="2,4",
        help="niveaux de parallélisme à mesurer, séparés par des virgules",
    )
    args = parser.parse_args()

    modele = MODELES[args.modele]
    if not modele.exists():
        raise SystemExit(f"modèle absent : {modele}")

    print(f"Banc E2 — {args.modele}, {args.fils} fils, {args.creneaux} créneaux")
    print("Démarrage du serveur (chargement du modèle)…")
    with Serveur(modele, args.fils, args.creneaux):
        essais = joue(args.modele, args.fils)
        charges = []
        for niveau in [int(n) for n in args.charges.split(",") if n.strip()]:
            niveau = min(niveau, args.creneaux)
            print(f"  charge à {niveau} appels simultanés …", end="", flush=True)
            mesure = charge_parallele(niveau)
            print(f" {mesure['debit_agrege']:.1f} tok/s agrégés")
            charges.append(mesure)

    rapport = {
        "modele": args.modele,
        "fils": args.fils,
        "creneaux": args.creneaux,
        "essais": essais,
        "charges": charges,
    }
    resume(rapport)

    sortie = RACINE / "resultats" / f"{args.modele}.json"
    sortie.write_text(json.dumps(rapport, ensure_ascii=False, indent=2))
    print(f"\nDétail écrit dans {sortie}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
