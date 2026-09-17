"""Chantier D / D0 (suite 4b) — séparation nette, ou continuum ?

Les deux régimes mesurés jusqu'ici (net 0,85/0,15 et flou 0,62/0,38) ne disent pas si le
plan (indicateur, résidu) est coupé en deux ou continu : rien n'a été généré entre eux.
On comble l'intervalle, et on regarde.
"""
import json, sys
import numpy as np
from sklearn.metrics import silhouette_score
from reddwarf.implementations.polis import run_pipeline
sys.path.insert(0, "/home/ubuntu/red-dwarf-test/d0_mesures")
import mesures_d0_suite2 as m2
from mesures_d0_suite2 import aligne_procruste, coords, partition, jaccard_apparie, deplacement

RS = 42
GRAINES = (20260903, 7, 1234, 99991)
# du flou au net, par pas réguliers ; passe et couverture interpolées de même
NIVEAUX = [(0.62, 0.18, 0.70), (0.67, 0.16, 0.75), (0.72, 0.13, 0.80),
           (0.77, 0.11, 0.85), (0.85, 0.08, 0.90)]


def mesure(p_haut, passe, couverture, graine):
    reglages = {"p_haut": p_haut, "p_bas": round(1 - p_haut, 2),
                "passe": passe, "couverture": couverture}
    v_av, v_ap, communs = m2.construis_corpus(graine, reglages)
    r_av = run_pipeline(votes=v_av, random_state=RS)
    r_ap = run_pipeline(votes=v_ap, random_state=RS)

    var = np.asarray(r_av.reducer.explained_variance_, dtype=float)
    G = float((var[0] - var[1]) / var[0])
    sub = r_av.participants_df[r_av.participants_df["cluster_id"].notna()]
    S = float(silhouette_score(sub[["x", "y"]].to_numpy(),
                               sub["cluster_id"].astype(int).to_numpy()))

    Xav, _ = coords(r_av, communs)
    Xap, _ = coords(r_ap, communs)
    t, _ = aligne_procruste(Xav, Xap)
    residu = deplacement(t(Xav), Xap)

    centres = t(r_av.clusterer.cluster_centers_).tolist()
    r_ch = run_pipeline(votes=v_ap, random_state=RS, init_centers=centres)
    p_av, p_fr, p_ch = partition(r_av), partition(r_ap), partition(r_ch)
    paires, _ = jaccard_apparie(p_av, p_ch, set(communs))
    conserves = sum(1 for a, b, _, _ in paires if a == b)
    return {"p_haut": p_haut, "couverture": couverture, "graine": graine,
            "G": round(G, 4), "S": round(S, 4),
            "k_avant": int(sub["cluster_id"].nunique()),
            "residu_%": residu,
            "jaccard_froid_vs_chaud": round(jaccard_apparie(p_fr, p_ch)[1], 4),
            "identite": f"{conserves}/{len(paires)}"}


pts = [mesure(ph, pa, co, g) for (ph, pa, co) in NIVEAUX for g in GRAINES]
print(json.dumps(pts, indent=2, ensure_ascii=False))
