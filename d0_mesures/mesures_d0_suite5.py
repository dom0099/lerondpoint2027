"""Chantier D / D0 (suite 5) — les deux derniers trous.

  1. un prédicteur de la STABILITÉ DE COMPOSITION existe-t-il ?
     candidats déjà disponibles : G (écart des valeurs propres) et |k_après - k_avant|.
  2. quel est le BRUIT de S près du seuil 0,456 ?
     ré-échantillonnage du jeu « avant » sur des configurations dont S est dans [0,40 ; 0,50].
"""
import json, sys
import numpy as np
from scipy.stats import spearmanr
from sklearn.metrics import silhouette_score
from reddwarf.implementations.polis import run_pipeline

sys.path.insert(0, "/home/ubuntu/red-dwarf-test/d0_mesures")
import mesures_d0_suite2 as m2
from mesures_d0_suite2 import aligne_procruste, coords, partition, jaccard_apparie

RS = 42
D = "/home/ubuntu/red-dwarf-test/d0_mesures/"
GRAINES = (20260903, 7, 1234, 99991)
NIVEAUX = [(0.62, 0.18, 0.70), (0.67, 0.16, 0.75), (0.72, 0.13, 0.80),
           (0.77, 0.11, 0.85), (0.85, 0.08, 0.90)]


def reglages(p_haut, passe, couv):
    return {"p_haut": p_haut, "p_bas": round(1 - p_haut, 2), "passe": passe, "couverture": couv}


def silhouette_de(res):
    sub = res.participants_df[res.participants_df["cluster_id"].notna()]
    k = int(sub["cluster_id"].nunique())
    if k < 2:
        return float("nan"), k
    return float(silhouette_score(sub[["x", "y"]].to_numpy(),
                                  sub["cluster_id"].astype(int).to_numpy())), k


# ------------------------------------------------- 1. prédire la composition ?
def partie_1():
    s4b = {(x["p_haut"], x["graine"]): x for x in json.load(open(D + "resultats_d0_suite4b.json"))}
    pts = []
    for (ph, pa, co) in NIVEAUX:
        for g in GRAINES:
            v_av, v_ap, communs = m2.construis_corpus(g, reglages(ph, pa, co))
            r_av = run_pipeline(votes=v_av, random_state=RS)
            r_ap = run_pipeline(votes=v_ap, random_state=RS)
            S, k_av = silhouette_de(r_av)
            var = np.asarray(r_av.reducer.explained_variance_, dtype=float)
            G = float((var[0] - var[1]) / var[0])
            k_ap = len(partition(r_ap))
            pts.append({"jeu": f"p{ph}/{g}", "S": round(S, 4), "G": round(G, 4),
                        "k_avant": k_av, "k_apres": k_ap, "delta_k": abs(k_ap - k_av),
                        "J_froid_chaud": s4b[(ph, g)]["jaccard_froid_vs_chaud"],
                        "residu_%": s4b[(ph, g)]["residu_%"]})

    # l'échantillon réel, pour mémoire (k 2 -> 4, J = 0,3571)
    A = json.load(open(D + "resultats_d0_suite3.json"))["A_donnees_reelles"]
    pts.append({"jeu": "REEL Pol.is", "S": 0.2996, "G": 0.2195,
                "k_avant": A["k_avant"], "k_apres": A["k_apres"],
                "delta_k": abs(A["k_apres"] - A["k_avant"]),
                "J_froid_chaud": A["jaccard_froid_vs_chaud"], "residu_%": 46.8})

    J = np.array([p["J_froid_chaud"] for p in pts])
    res = {"n": len(pts),
           "spearman_G_vs_J": round(float(spearmanr([p["G"] for p in pts], J)[0]), 3),
           "spearman_deltak_vs_J": round(float(spearmanr([p["delta_k"] for p in pts], J)[0]), 3),
           "spearman_S_vs_J": round(float(spearmanr([p["S"] for p in pts], J)[0]), 3),
           "spearman_residu_vs_J": round(float(spearmanr([p["residu_%"] for p in pts], J)[0]), 3)}

    def separation(cle, sens):
        """meilleur seuil pour distinguer composition stable (J>=0,9) du reste."""
        vals = sorted({p[cle] for p in pts})
        best = None
        for i in range(len(vals) - 1):
            s = (vals[i] + vals[i + 1]) / 2
            err = sum(1 for p in pts
                      if ((p[cle] >= s) if sens > 0 else (p[cle] < s)) != (p["J_froid_chaud"] >= 0.9))
            if best is None or err < best[1]:
                best = (round(s, 4), err)
        return {"seuil": best[0], "erreurs": best[1], "n": len(pts),
                "exactitude_%": round(100 * (len(pts) - best[1]) / len(pts), 1)}

    res["separation_G_pour_J>=0.9"] = separation("G", +1)
    res["separation_deltak_pour_J>=0.9"] = separation("delta_k", -1)
    res["separation_S_pour_J>=0.9"] = separation("S", +1)
    res["repartition_delta_k"] = {}
    for dk in sorted({p["delta_k"] for p in pts}):
        v = [p["J_froid_chaud"] for p in pts if p["delta_k"] == dk]
        res["repartition_delta_k"][f"delta_k={dk}"] = {
            "n": len(v), "J_min": round(min(v), 4), "J_med": round(float(np.median(v)), 4),
            "J_max": round(max(v), 4), "stables_J>=0.9": sum(1 for x in v if x >= 0.9)}
    return {"points": pts, "resume": res}


# ------------------------------------------------------- 2. bruit de S au seuil
def reechantillonne(votes, rng, mode):
    if mode == "participants":
        par = {}
        for v in votes:
            par.setdefault(v["participant_id"], []).append(v)
        ids = list(par)
        tirage = rng.choice(ids, size=len(ids), replace=True)
        out = []
        for neuf, ancien in enumerate(tirage):
            for v in par[ancien]:
                out.append({**v, "participant_id": neuf})
        return out
    else:  # retrait de 10 % des votes
        garde = rng.random(len(votes)) > 0.10
        return [v for v, g in zip(votes, garde) if g]


def partie_2(cibles, n_rep=12):
    out = []
    for (ph, pa, co, g) in cibles:
        v_av, _, _ = m2.construis_corpus(g, reglages(ph, pa, co))
        S0, k0 = silhouette_de(run_pipeline(votes=v_av, random_state=RS))
        for mode in ("participants", "retrait10%"):
            rng = np.random.default_rng(12345)
            vals, ks = [], []
            for _ in range(n_rep):
                vr = reechantillonne(v_av, rng, mode)
                S, k = silhouette_de(run_pipeline(votes=vr, random_state=RS))
                if not np.isnan(S):
                    vals.append(round(S, 4)); ks.append(k)
            out.append({"jeu": f"p{ph}/{g}", "S_reference": round(S0, 4), "k_reference": k0,
                        "mode": mode, "n": len(vals),
                        "S_min": min(vals), "S_median": round(float(np.median(vals)), 4),
                        "S_max": max(vals), "etendue": round(max(vals) - min(vals), 4),
                        "franchit_0.456": bool(min(vals) < 0.456 <= max(vals)),
                        "k_observes": sorted(set(ks)), "valeurs": vals})
    return out


if __name__ == "__main__":
    cibles = [(0.67, 0.16, 0.75, 20260903),   # S = 0,438
              (0.62, 0.18, 0.70, 7),          # S = 0,446
              (0.77, 0.11, 0.85, 99991)]      # S = 0,464
    print(json.dumps({"partie_1_composition": partie_1(),
                      "partie_2_bruit_de_S": partie_2(cibles)},
                     indent=2, ensure_ascii=False))
