"""Importe de vraies conversations Pol.is publiques comme cas d'étude (chantier E2).

    python3 e2_mesures/import_polis.py > /home/ubuntu/chantier-e2/cas_polis.json

**Ce que ces cas apportent, et ce qu'ils n'apportent pas.** Ils apportent des
déclarations écrites par de vrais participants — que personne dans ce projet n'a
rédigées — et des groupes calculés par le vrai Pol.is, jusqu'à quatre par conversation
là où nos débats n'en produisent que deux. C'est la robustesse de FORME qu'ils
éprouvent : un modèle qui hallucine des groupes ou renumérote par position le fera
d'autant plus qu'il y a de groupes.

Ils n'apportent **pas** de vérité de terrain : personne n'a écrit ce que chaque groupe
pense vraiment. On ne peut donc pas s'en servir pour dire qu'un nom est faux, seulement
pour voir si le format tient et si les noms changent quand on permute les groupes. Et ils
ne sont pas en français, donc ils ne disent rien de la collision « Oui/Non ». La justesse
se juge sur les débats français, la forme sur ceux-ci.

La `repness` de Pol.is porte le même `repful-for` que la nôtre — même notion, même usage.
"""

import json
import sys
import time
import urllib.request

#: Pol.is refuse l'agent par défaut de `urllib` (403). On se nomme donc explicitement,
#: plutôt que de se déguiser en navigateur : ces requêtes sont automatiques, autant que
#: ça se voie dans leurs journaux. Une pause sépare les appels — quatre conversations ne
#: justifient aucune hâte, et un service public gratuit n'a pas à absorber nos rafales.
AGENT = "chantier-c/e2 (recherche, lerondpoint2027.fr)"
PAUSE = 1.0

#: Conversations publiques déjà utilisées par le banc de comparaison red-dwarf du
#: chantier B (`~/red-dwarf-test/run_public.py`) : elles sont donc déjà dans le
#: périmètre de ce projet, et leur choix ne doit rien à ce que les modèles en feront.
CONVERSATIONS = ["3ntrtcehas", "4asymkcrjf", "4cvkai2ctw", "2dhnep37ie"]
N_DECLARATIONS = 5


def json_de(url: str):
    requete = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(requete, timeout=60) as reponse:
        charge = json.loads(reponse.read())
    time.sleep(PAUSE)
    return charge


def importe(cid: str) -> dict | None:
    maths = json_de(f"https://pol.is/api/v3/math/pca2?conversation_id={cid}")
    commentaires = json_de(f"https://pol.is/api/v3/comments?conversation_id={cid}")
    textes = {c["tid"]: c["txt"] for c in commentaires}
    langues = [c.get("lang") for c in commentaires if c.get("lang")]

    repness = maths.get("repness") or {}
    tailles = {str(g["id"]): len(g.get("members", [])) for g in maths.get("group-clusters", [])}
    groupes = []
    for gid, lignes in sorted(repness.items(), key=lambda kv: int(kv[0])):
        declarations = []
        # `repness` est déjà rendue triée par Pol.is, du plus au moins représentatif.
        for ligne in lignes[:N_DECLARATIONS]:
            texte = textes.get(ligne["tid"])
            if not texte:
                continue
            declarations.append(
                [texte, "pour" if ligne.get("repful-for") == "agree" else "contre"]
            )
        if declarations:
            groupes.append(
                {
                    "id": int(gid),
                    # Aucune vérité de terrain : personne n'a écrit ce que ce groupe
                    # pense. Le champ existe pour que le format du corpus soit le même
                    # partout, et il vaut None pour que rien ne puisse le prendre pour
                    # une référence.
                    "camp_majoritaire": None,
                    "effectif": tailles.get(gid),
                    "declarations": declarations,
                }
            )
    if len(groupes) < 2:
        return None
    return {
        "cle": f"polis-{cid}",
        "titre": f"Conversation Pol.is {cid}",
        "langue": max(set(langues), key=langues.count) if langues else "?",
        "groupes": groupes,
    }


def main() -> int:
    cas = []
    for cid in CONVERSATIONS:
        try:
            resultat = importe(cid)
        except Exception as erreur:  # noqa: BLE001 — un cas manquant n'arrête pas le lot
            print(f"  {cid} : échec ({erreur})", file=sys.stderr)
            continue
        if resultat is None:
            print(f"  {cid} : moins de deux groupes, écarté", file=sys.stderr)
            continue
        print(
            f"  {cid} : {len(resultat['groupes'])} groupes, langue {resultat['langue']}",
            file=sys.stderr,
        )
        cas.append(resultat)
    print(json.dumps(cas, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
