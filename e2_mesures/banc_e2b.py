"""Deuxième passe du E2 : invite v2 + grammaire, sur le corpus élargi.

    python3 e2_mesures/banc_e2b.py --modele mistral

Reprend le dispositif du `banc_e2.py` (serveur llama.cpp local, `nice`, rien d'installé)
et ne change que ce que la première passe a mis en cause :

- **l'invite v2**, qui range les propositions sous « ce groupe approuve / rejette » et
  les met entre guillemets, au lieu de la ligne `CONTRE : Oui à la campagne` où
  l'annotation contredisait le texte mot à mot ;
- **une grammaire GBNF par débat**, qui rend impossibles — et non plus seulement
  improbables — les trois défauts de format mesurés : groupes inventés, renumérotation
  par ordre de présentation, troncature par bavardage ;
- **un corpus de treize débats** au lieu de cinq, dont quatre passés par le vrai moteur
  et quatre vraies conversations Pol.is.

Le reste est identique, exprès : deux ordres de présentation, même graine, même
température, même machine. Une comparaison où l'on change tout ne compare rien.
"""

import argparse
import json
import pathlib
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from banc_e2 import BASE, MODELES, RACINE, Serveur  # noqa: E402
from corpus2 import charge  # noqa: E402
from invite import SYSTEME_V2, grammaire, invite_v2, json_pur, lit_reponse  # noqa: E402


def appelle(message: str, gbnf: str, jetons: int = 400) -> dict:
    charge_utile = json.dumps(
        {
            "messages": [
                {"role": "system", "content": SYSTEME_V2},
                {"role": "user", "content": message},
            ],
            "max_tokens": jetons,
            "temperature": 0.2,
            "seed": 42,
            "grammar": gbnf,
        }
    ).encode()
    requete = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=charge_utile,
        headers={"Content-Type": "application/json"},
    )
    debut = time.monotonic()
    with urllib.request.urlopen(requete, timeout=1800) as reponse:
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


def joue(debats) -> list[dict]:
    resultats = []
    for debat in debats:
        ids = [g["id"] for g in debat["groupes"]]
        gbnf = grammaire(debat)
        for etiquette, ordre in (("direct", ids), ("inverse", list(reversed(ids)))):
            print(f"  {debat['cle']:<22} {etiquette:<8} …", end="", flush=True)
            essai = appelle(invite_v2(debat, ordre=ordre), gbnf)
            noms = lit_reponse(essai["sortie"])
            manquants = [i for i in ids if not noms.get(i, {}).get("nom")]
            print(
                f" {essai['secondes']:>6.1f} s"
                f"  {essai['debit'] or 0:>5.1f} tok/s"
                f"  {len(ids) - len(manquants)}/{len(ids)} noms"
            )
            resultats.append(
                {
                    "debat": debat["cle"],
                    "provenance": debat["provenance"],
                    "verite": debat["verite"],
                    "ordre": etiquette,
                    "n_groupes": len(ids),
                    "noms": {
                        str(g["id"]): {
                            "position_attendue": g.get("position"),
                            "reference": g.get("reference"),
                            **noms.get(g["id"], {"nom": "", "justification": ""}),
                        }
                        for g in debat["groupes"]
                    },
                    **{c: essai[c] for c in ("secondes", "jetons", "debit", "json_pur")},
                    "brut": essai["sortie"],
                }
            )
    return resultats


def resume(rapport: dict) -> None:
    essais = rapport["essais"]
    debits = [e["debit"] for e in essais if e["debit"]]
    complets = sum(1 for e in essais if all(n["nom"] for n in e["noms"].values()))
    purs = sum(1 for e in essais if e["json_pur"])
    groupes_attendus = sum(e["n_groupes"] for e in essais)
    groupes_nommes = sum(
        1 for e in essais for n in e["noms"].values() if n["nom"]
    )

    print(f"\n=== {rapport['modele']} — invite v2 + grammaire ===")
    print(f"  essais                   : {len(essais)}")
    print(f"  réponses complètes       : {complets}/{len(essais)}")
    print(f"  groupes nommés           : {groupes_nommes}/{groupes_attendus}")
    print(f"  JSON nu, sans enrobage   : {purs}/{len(essais)}")
    if debits:
        print(f"  débit médian             : {sorted(debits)[len(debits)//2]:.1f} tok/s")
    jetons = [e["jetons"] for e in essais if e["jetons"]]
    if jetons:
        par_groupe = [
            e["jetons"] / e["n_groupes"] for e in essais if e["jetons"]
        ]
        print(f"  jetons par groupe (méd.) : {sorted(par_groupe)[len(par_groupe)//2]:.0f}"
              f"   (v1 : 65 à 86 ; hypothèse du 5 sept. : 30)")


def main() -> int:
    parser = argparse.ArgumentParser(prog="banc_e2b")
    parser.add_argument("--modele", choices=sorted(MODELES), required=True)
    parser.add_argument("--fils", type=int, default=6)
    parser.add_argument("--creneaux", type=int, default=4)
    args = parser.parse_args()

    modele = MODELES[args.modele]
    if not modele.exists():
        raise SystemExit(f"modèle absent : {modele}")
    debats = charge()

    print(f"Banc E2b — {args.modele}, {len(debats)} débats, invite v2 + grammaire")
    with Serveur(modele, args.fils, args.creneaux):
        essais = joue(debats)

    rapport = {"modele": args.modele, "version": 2, "fils": args.fils, "essais": essais}
    resume(rapport)
    sortie = RACINE / "resultats" / f"{args.modele}-v2.json"
    sortie.write_text(json.dumps(rapport, ensure_ascii=False, indent=2))
    print(f"\nDétail écrit dans {sortie}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
