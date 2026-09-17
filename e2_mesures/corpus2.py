"""Le corpus élargi du E2 (deuxième passe) : treize débats, trois provenances.

Le premier E2 a échoué sur son corpus autant que sur ses modèles : quatre débats sur
cinq étaient écrits à la main, et c'est moi qui y choisissais les déclarations
représentatives de chaque groupe. Les modèles s'y débrouillaient bien et s'effondraient
sur le seul débat réel. **Un corpus écrit par celui qui écrit l'invite mesure la facilité
de son invite, pas la difficulté de la tâche.**

Trois provenances, et chacune répond à une question que les autres ne peuvent pas poser.

| Provenance | Ce qu'elle éprouve | Vérité de terrain |
| --- | --- | --- |
| `production` — le débat réel du permis à 16 ans | le cas qui a tout mis en échec | oui, relevée à la main |
| `pipeline` — 4 débats français passés par red-dwarf | la justesse en français, sur une `repness` que l'algorithme a choisie | oui, par camp majoritaire |
| `polis` — 4 conversations Pol.is publiques | la robustesse de forme : 3 et 4 groupes, déclarations de vrais participants | **non** |
| `main` — les 4 débats écrits au premier E2 | conservés pour comparer v1 et v2 sur le même terrain | oui, mais suspecte |

Les cas `polis` n'ont aucune vérité de terrain — personne n'a écrit ce que chaque groupe
pense. Ils ne peuvent donc pas dire qu'un nom est faux, seulement si le format tient et
si les noms résistent à une permutation des groupes. C'est écrit dans leur champ
`verite`, pour qu'aucun dépouillement ne les compte par erreur dans la justesse.
"""

import json
import pathlib

import corpus as corpus_v1

CAS = pathlib.Path("/home/ubuntu/chantier-e2")


def _depuis_v1(debat: dict) -> dict:
    """Les débats du premier E2, ramenés au format commun (tuples -> listes)."""
    return {
        "cle": debat["cle"],
        "titre": debat["titre"],
        "provenance": "production" if debat["reel"] else "main",
        "verite": True,
        "groupes": [
            {
                "id": g["id"],
                "reference": g["reference"],
                "position": g["position"],
                "declarations": [[t, "pour" if s == "agree" else s] for t, s in g["declarations"]],
            }
            for g in debat["groupes"]
        ],
    }


def _depuis_fichier(nom: str, provenance: str, verite: bool) -> list[dict]:
    chemin = CAS / nom
    if not chemin.exists():
        return []
    debats = []
    for brut in json.loads(chemin.read_text()):
        debats.append(
            {
                "cle": brut["cle"],
                "titre": brut["titre"],
                "provenance": provenance,
                "verite": verite,
                "langue": brut.get("langue", "fr"),
                "groupes": [
                    {
                        "id": g["id"],
                        # Pour les débats du pipeline, la référence est le camp que le
                        # découpage a majoritairement rassemblé — constatée après le
                        # calcul, jamais décidée avant.
                        "reference": g.get("camp_majoritaire"),
                        "position": g.get("camp_majoritaire"),
                        "effectif": g.get("effectif"),
                        "declarations": g["declarations"],
                    }
                    for g in brut["groupes"]
                ],
            }
        )
    return debats


def charge() -> list[dict]:
    debats = [_depuis_v1(d) for d in corpus_v1.DEBATS]
    debats += _depuis_fichier("cas_pipeline.json", "pipeline", verite=True)
    debats += _depuis_fichier("cas_polis.json", "polis", verite=False)
    return debats


if __name__ == "__main__":
    for debat in charge():
        print(
            f"{debat['cle']:<22} {debat['provenance']:<11} "
            f"{len(debat['groupes'])} groupes  "
            f"{'vérité' if debat['verite'] else 'sans vérité'}"
        )
