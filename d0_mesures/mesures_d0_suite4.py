"""Chantier D / D0 (suite 4) — un indicateur prédit-il le régime ?

Analyse a posteriori : les résidus et Jaccard sont RELUS des JSON déjà produits.
Seuls les runs « avant » (50 participants) sont rejoués, parce que les indicateurs
n'avaient pas été enregistrés. Deux candidats, tous deux disponibles AVANT tout
alignement et sans connaître le jeu « après » :

    G = (lambda1 - lambda2) / lambda1     écart relatif des deux valeurs propres
    S = silhouette du meilleur k trouvé sur le jeu « avant »
"""

import csv, json, sys
import numpy as np
from sklearn.metrics import silhouette_score

from reddwarf.implementations.polis import run_pipeline
sys.path.insert(0, "/home/ubuntu/red-dwarf-test/d0_mesures")
import mesures_d0_suite2 as m2

RANDOM_STATE = 42
D = "/home/ubuntu/red-dwarf-test/d0_mesures/"
GRAINES_BALAYAGE = (20260903, 7, 1234)


def indicateurs(votes):
    """Calculés sur le SEUL jeu « avant » : rien du futur n'y entre."""
    r = run_pipeline(votes=votes, random_state=RANDOM_STATE)
    var = np.asarray(r.reducer.explained_variance_, dtype=float)
    ratio = np.asarray(r.reducer.explained_variance_ratio_, dtype=float)
    G = float((var[0] - var[1]) / var[0])
    df = r.participants_df
    sub = df[df["cluster_id"].notna()]
    k = int(sub["cluster_id"].nunique())
    S = float(silhouette_score(sub[["x", "y"]].to_numpy(),
                               sub["cluster_id"].astype(int).to_numpy())) if k > 1 else float("nan")
    return {"G_ecart_valeurs_propres": round(G, 4),
            "S_silhouette_avant": round(S, 4),
            "k_avant": k,
            "variance_expliquee_2C_%": round(100 * float(ratio.sum()), 1)}


def main():
    s2 = json.load(open(D + "resultats_d0_suite2.json"))
    s3 = json.load(open(D + "resultats_d0_suite3.json"))
    points = []

    # --- 1. les 8 configurations principales (4 graines x 2 régimes) -------------
    for r in s2:
        v_av, _, _ = m2.construis_corpus(r["graine"], m2.CORPUS[r["corpus"]])
        ind = indicateurs(v_av)
        m3 = r["mesure_3"]
        ap = m3["identite_chaud_auto"]["appariement"]
        conserves = sum(1 for a, b in ap.items() if int(a) == int(b))
        points.append({
            "jeu": f"{r['corpus']}/{r['graine']}",
            "regime_attendu": r["corpus"],
            "couverture": m2.CORPUS[r["corpus"]]["couverture"],
            **ind,
            "residu_auto_%": r["deplacement_%_rayon"]["ALIGNEMENT AUTO (Procruste)"],
            "identite_auto": f"{conserves}/{len(ap)}",
            "jaccard_froid_vs_chaud_auto": m3["jaccard_froid_vs_chaud_auto"],
        })

    # --- 2. le balayage de couverture (résidus relus, indicateurs recalculés) ----
    for entree in s3["B2_balayages"]["couverture"]:
        reglages = dict(m2.CORPUS[entree["corpus"]], couverture=entree["couverture"])
        for i, graine in enumerate(GRAINES_BALAYAGE):
            v_av, _, _ = m2.construis_corpus(graine, reglages)
            ind = indicateurs(v_av)
            points.append({
                "jeu": f"{entree['corpus']}/couv{entree['couverture']}/{graine}",
                "regime_attendu": entree["corpus"],
                "couverture": entree["couverture"],
                **ind,
                "residu_auto_%": entree["residus"][i],
                "identite_auto": None,
                "jaccard_froid_vs_chaud_auto": None,
            })

    # --- 3. l'échantillon réel Pol.is (reconstruit en mémoire, rien sur disque) --
    lignes = [l for l in open("/home/ubuntu/red-dwarf-test/votes_2dcdfr5fbi.csv") if "," in l]
    brut = [x for x in csv.DictReader(lignes) if x.get("participant_id", "").strip().isdigit()]
    index, pseudo = {}, []
    for x in sorted(brut, key=lambda x: int(x["modified"])):
        pid = index.setdefault(x["participant_id"], len(index))
        pseudo.append({"participant_id": pid, "statement_id": int(x["statement_id"]),
                       "vote": int(x["vote"]), "modified": int(x["modified"])})
    coupe = pseudo[: (2 * len(pseudo)) // 3]
    A = s3["A_donnees_reelles"]
    points.append({
        "jeu": "REEL Pol.is (14 ptpt)",
        "regime_attendu": "réel",
        "couverture": 1.0,
        **indicateurs(coupe),
        "residu_auto_%": A["deplacement_%"]["ALIGNEMENT AUTO"],
        "identite_auto": "1/2",
        "jaccard_froid_vs_chaud_auto": A["jaccard_froid_vs_chaud"],
    })

    # --- 4. séparation : un seuil existe-t-il ? ---------------------------------
    def meilleur_seuil(cle, cible=lambda p: p["residu_auto_%"] < 20.0):
        vals = sorted({p[cle] for p in points if not np.isnan(p[cle])})
        best = None
        for i in range(len(vals) - 1):
            seuil = (vals[i] + vals[i + 1]) / 2
            err = sum(1 for p in points
                      if not np.isnan(p[cle]) and (p[cle] >= seuil) != cible(p))
            if best is None or err < best[1]:
                best = (round(seuil, 4), err)
        n = sum(1 for p in points if not np.isnan(p[cle]))
        return {"seuil": best[0], "erreurs": best[1], "n": n,
                "exactitude_%": round(100 * (n - best[1]) / n, 1)}

    resume = {
        "n_points": len(points),
        "separation_G": meilleur_seuil("G_ecart_valeurs_propres"),
        "separation_S": meilleur_seuil("S_silhouette_avant"),
        "plages": {},
    }
    for cle in ("G_ecart_valeurs_propres", "S_silhouette_avant", "residu_auto_%"):
        for reg in ("net", "flou", "réel"):
            v = [p[cle] for p in points if p["regime_attendu"] == reg]
            resume["plages"][f"{cle} / {reg}"] = [round(min(v), 4), round(max(v), 4)]

    print(json.dumps({"points": points, "resume": resume}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
