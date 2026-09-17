"""Lecture des résultats du banc E2 : justesse, biais de position, débit.

    python3 e2_mesures/analyse.py mistral qwen

Séparé du banc pour une raison pratique : un passage coûte une vingtaine de minutes sur
cette machine, et on veut pouvoir relire ses résultats autant de fois qu'il faut sans le
rejouer. Les fichiers de `/home/ubuntu/chantier-e2/resultats/` sont la mesure ; ceci n'en
est que la lecture.
"""

import json
import pathlib
import re
import sys

RESULTATS = pathlib.Path("/home/ubuntu/chantier-e2/resultats")

#: Marqueurs de position dans un nom français. Grossier par construction, et c'est
#: assumé : ils ne servent pas à noter le modèle mais à SIGNALER les inversions à
#: relire. Un nom qui contient « opposants » pour un groupe dont la position attendue
#: est « pour » est presque sûrement faux ; c'est un humain qui tranche, pas ce tableau.
#:
#: **Et il a fallu que ce soit un humain.** Au premier dépouillement, ce compteur a
#: annoncé « 0 inversion » pour Qwen — alors qu'il avait inverti LES DEUX groupes du
#: seul débat réel. Deux angles morts, tous deux normaux pour une expression régulière :
#: les faux négatifs, quand le nom ne porte aucun marqueur (« Sécurité avant tout »
#: pour un groupe favorable, la position n'étant que dans la justification) ; et les
#: faux positifs, quand un marqueur trompe (« défenseurs du paysage » compté « pour »
#: alors que ce groupe est contre le projet). Le chiffre de ce tableau ne doit donc
#: JAMAIS être cité seul : il ouvre la relecture, il ne la remplace pas. C'est
#: exactement le genre de raccourci qui ferait trancher le E3 dans le mauvais sens.
POUR = re.compile(
    r"\b(pour|favorables?|partisans?|défenseurs?|soutiens?|en faveur|pro-)\b", re.I
)
CONTRE = re.compile(
    r"\b(contre|opposants?|opposés?|hostiles?|réfractaires?|anti-|refus|sceptiques?)\b",
    re.I,
)


def polarite(nom: str) -> str:
    """« pour », « contre », ou « indéterminée » si le nom ne se prononce pas."""
    a_pour, a_contre = bool(POUR.search(nom)), bool(CONTRE.search(nom))
    if a_pour and not a_contre:
        return "pour"
    if a_contre and not a_pour:
        return "contre"
    return "indéterminée"


def par_ordre(rapport: dict) -> dict:
    """(débat, ordre) -> {id de groupe: nom}."""
    table = {}
    for essai in rapport["essais"]:
        table[(essai["debat"], essai["ordre"])] = {
            gid: donnees["nom"] for gid, donnees in essai["noms"].items()
        }
    return table


def analyse(modele: str) -> None:
    chemin = RESULTATS / f"{modele}.json"
    if not chemin.exists():
        print(f"— {modele} : pas de résultat ({chemin})")
        return
    rapport = json.loads(chemin.read_text())
    table = par_ordre(rapport)

    print(f"\n{'=' * 78}\n  {modele.upper()}  —  {rapport['fils']} fils, "
          f"{rapport.get('creneaux', '?')} créneaux\n{'=' * 78}")

    inversions, muets, instables, total = 0, 0, 0, 0
    for essai in rapport["essais"]:
        if essai["ordre"] != "direct":
            continue
        autre = table.get((essai["debat"], "inverse"), {})
        marque = " (réel)" if essai["reel"] else ""
        print(f"\n### {essai['debat']}{marque}")
        for gid, donnees in sorted(essai["noms"].items(), key=lambda kv: int(kv[0])):
            total += 1
            nom = donnees["nom"]
            attendue = donnees["position_attendue"]
            vue = polarite(nom)
            nom_inverse = autre.get(gid, "")
            stable = nom.strip().lower() == nom_inverse.strip().lower()

            drapeaux = []
            if not nom:
                drapeaux.append("VIDE")
                muets += 1
            elif attendue in ("pour", "contre") and vue != "indéterminée" and vue != attendue:
                drapeaux.append("INVERSION")
                inversions += 1
            if nom and nom_inverse and not stable:
                drapeaux.append("instable")
                instables += 1

            print(f"  [{gid}] attendu : {donnees['reference']} ({attendue})")
            print(f"      direct  : {nom or '—'}")
            print(f"      inverse : {nom_inverse or '—'}")
            if drapeaux:
                print(f"      >>> {', '.join(drapeaux)}")

    print(f"\n--- {modele} : {total} groupes ---")
    print(f"  inversions de position    : {inversions}")
    print(f"  noms manquants            : {muets}")
    print(f"  noms changeant avec l'ordre: {instables}   "
          f"(biais de position s'il est élevé)")

    debits = [e["debit"] for e in rapport["essais"] if e["debit"]]
    if debits:
        print(f"  débit médian (1 appel)    : {sorted(debits)[len(debits)//2]:.1f} tok/s")
    for charge in rapport.get("charges", []):
        print(f"  {charge['creneaux']} en parallèle           : "
              f"{charge['debit_agrege']:.1f} tok/s agrégés")


def main() -> int:
    modeles = sys.argv[1:] or ["mistral", "qwen"]
    for modele in modeles:
        analyse(modele)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
