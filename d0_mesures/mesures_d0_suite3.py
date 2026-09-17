"""Chantier D / D0 (suite 3).

  A. protocole sur les vrais votes Pol.is disponibles localement (coupe chronologique) ;
  B. caractérisation du résidu du corpus flou : imputation ? scaler ? structure ?
     - B1 : variantes de pipeline (complet / sans scaler / remplissage par 0) + angles
            principaux entre les sous-espaces PCA « avant » et « après » ;
     - B2 : balayage du taux de couverture, et balayage du nombre de participants.

Lecture/expérimentation seule.
"""

import csv, json, sys
import numpy as np
from sklearn.decomposition import PCA
from sklearn.impute import SimpleImputer

from reddwarf.implementations.polis import run_pipeline
from reddwarf.utils.matrix import generate_raw_matrix, simple_filter_matrix

sys.path.insert(0, "/home/ubuntu/red-dwarf-test/d0_mesures")
from mesures_d0_suite2 import (aligne_procruste, coords, partition, jaccard_apparie,
                               deplacement, flip, construis_corpus, CORPUS,
                               N_PTPT_AVANT, N_DECL_AVANT, N_GROUPES)

RANDOM_STATE = 42


# ------------------------------------------------------------------ projections nues
def matrice(votes):
    brut = generate_raw_matrix(votes=votes)
    return simple_filter_matrix(vote_matrix=brut, mod_out_statement_ids=[])


def projette(votes, mode):
    """mode = 'moyenne' (imputation par la moyenne) ou 'zero' (remplissage par 0).
    Dans les deux cas : PCA(2) NUE, sans SparsityAwareScaler."""
    df = matrice(votes)
    X = df.values.astype(float)
    if mode == "moyenne":
        X = SimpleImputer(missing_values=np.nan, strategy="mean").fit_transform(X)
    else:
        X = np.nan_to_num(X, nan=0.0)
    pca = PCA(n_components=2, random_state=RANDOM_STATE)
    P = pca.fit_transform(X)
    return {int(p): P[i] for i, p in enumerate(df.index)}, pca, list(df.columns)


def coords_communs(proj, communs):
    return np.array([proj[p] for p in communs if p in proj])


def angles_principaux(pca_a, cols_a, pca_b, cols_b):
    """Angles principaux entre les deux plans PCA, restreints aux déclarations communes."""
    communs = [c for c in cols_a if c in set(cols_b)]
    ia = [list(cols_a).index(c) for c in communs]
    ib = [list(cols_b).index(c) for c in communs]
    A = np.linalg.qr(pca_a.components_[:, ia].T)[0]
    B = np.linalg.qr(pca_b.components_[:, ib].T)[0]
    s = np.clip(np.linalg.svd(A.T @ B, compute_uv=False), -1, 1)
    return [round(float(np.degrees(np.arccos(v))), 1) for v in s]


def residu(votes_av, votes_ap, communs, mode):
    pa, pca_a, ca = projette(votes_av, mode)
    pb, pca_b, cb = projette(votes_ap, mode)
    ids = [p for p in communs if p in pa and p in pb]
    A, B = np.array([pa[p] for p in ids]), np.array([pb[p] for p in ids])
    t, info = aligne_procruste(A, B)
    return deplacement(t(A), B), deplacement(A, B), info, (pca_a, ca, pca_b, cb)


# --------------------------------------------------------------- A. données réelles
def partie_A():
    lignes = [l for l in open("/home/ubuntu/red-dwarf-test/votes_2dcdfr5fbi.csv") if "," in l]
    brut = [r for r in csv.DictReader(lignes) if r.get("participant_id", "").strip().isdigit()]
    # Pseudonymisation : réindexation des comptes, on ne garde qu'index/déclaration/vote/date.
    index = {}
    pseudo = []
    for r in sorted(brut, key=lambda r: int(r["modified"])):
        pid = index.setdefault(r["participant_id"], len(index))
        pseudo.append({"participant_id": pid, "statement_id": int(r["statement_id"]),
                       "vote": int(r["vote"]), "modified": int(r["modified"])})
    with open("/home/ubuntu/red-dwarf-test/reel_pseudonymise.csv", "w", newline="") as f:
        w = csv.DictWriter(f, ["participant_id", "statement_id", "vote", "modified"])
        w.writeheader(); w.writerows(pseudo)

    coupe = pseudo[: (2 * len(pseudo)) // 3]           # deux premiers tiers, chronologiques
    av_ids = {v["participant_id"] for v in coupe}
    ap_ids = {v["participant_id"] for v in pseudo}
    r_av = run_pipeline(votes=coupe, random_state=RANDOM_STATE)
    r_ap = run_pipeline(votes=pseudo, random_state=RANDOM_STATE)
    r_ap2 = run_pipeline(votes=pseudo, random_state=RANDOM_STATE)

    Cav, ids_av = coords(r_av)
    Cap_all, ids_ap = coords(r_ap)
    communs = [p for p in ids_av if p in set(ids_ap)]
    A = np.array([Cav[ids_av.index(p)] for p in communs])
    B = np.array([Cap_all[ids_ap.index(p)] for p in communs])
    t, info = aligne_procruste(A, B)

    p_av, p_fr = partition(r_av), partition(r_ap)
    centres_auto = t(r_av.clusterer.cluster_centers_).tolist()
    r_chaud = run_pipeline(votes=pseudo, random_state=RANDOM_STATE, init_centers=centres_auto)
    p_ch = partition(r_chaud)
    U = set(communs)
    ap_av_fr, j_av_fr = jaccard_apparie(p_av, p_fr, U)
    ap_av_ch, j_av_ch = jaccard_apparie(p_av, p_ch, U)

    return {
        "source": "export Pol.is du 2026-09-01 (blog.lerondpoint2027.fr), déjà présent dans ~/red-dwarf-test",
        "votes_total": len(pseudo), "participants_total": len(ap_ids),
        "declarations": len({v["statement_id"] for v in pseudo}),
        "coupe_2_tiers": {"votes": len(coupe), "participants": len(av_ids)},
        "communs": len(communs),
        "temoin_bit_a_bit": bool(np.array_equal(Cap_all, coords(r_ap2)[0])),
        "k_avant": len(p_av), "k_apres": len(p_fr), "k_chaud": len(p_ch),
        "deplacement_%": {
            "sans correction": deplacement(A, B),
            "flip_x": deplacement(flip(A, True, False), B),
            "flip_x+flip_y (defaut biblio)": deplacement(flip(A, True, True), B),
            "ALIGNEMENT AUTO": deplacement(t(A), B),
        },
        "alignement": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in info.items()},
        "jaccard_froid_vs_chaud": round(jaccard_apparie(p_fr, p_ch)[1], 4),
        "jaccard_avant_vs_froid_communs": round(j_av_fr, 4),
        "jaccard_avant_vs_chaud_communs": round(j_av_ch, 4),
        "identite_froid": {str(a): int(b) for a, b, _, _ in ap_av_fr},
        "identite_chaud": {str(a): int(b) for a, b, _, _ in ap_av_ch},
    }


# ------------------------------------------------- B1. d'où vient le résidu flou ?
def partie_B1(graines=(20260903, 7, 1234)):
    out = []
    for nom in ("net", "flou"):
        for g in graines:
            v_av, v_ap, communs = construis_corpus(g, CORPUS[nom])
            r_moy, brut_moy, info_moy, (pa, ca, pb, cb) = residu(v_av, v_ap, communs, "moyenne")
            r_zero, brut_zero, _, _ = residu(v_av, v_ap, communs, "zero")
            # pipeline complet (avec SparsityAwareScaler), pour comparaison
            R_av = run_pipeline(votes=v_av, random_state=RANDOM_STATE)
            R_ap = run_pipeline(votes=v_ap, random_state=RANDOM_STATE)
            Xav, ids = coords(R_av, communs)
            Xap, _ = coords(R_ap, communs)
            t, _ = aligne_procruste(Xav, Xap)
            out.append({
                "corpus": nom, "graine": g,
                "residu_%_pipeline_complet": deplacement(t(Xav), Xap),
                "residu_%_pca_nue_imputation_moyenne": r_moy,
                "residu_%_pca_nue_remplissage_zero": r_zero,
                "angles_principaux_deg": angles_principaux(pa, ca, pb, cb),
            })
    return out


# ------------------------------ B2. le résidu suit-il la couverture ou la taille ?
def partie_B2(graines=(20260903, 7, 1234)):
    import mesures_d0_suite2 as m2
    balayages = {"couverture": [], "taille": []}

    for couv in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
        for nom in ("flou", "net"):
            reglages = dict(CORPUS[nom], couverture=couv)
            vals = []
            for g in graines:
                v_av, v_ap, communs = m2.construis_corpus(g, reglages)
                R_av = run_pipeline(votes=v_av, random_state=RANDOM_STATE)
                R_ap = run_pipeline(votes=v_ap, random_state=RANDOM_STATE)
                Xav, _ = coords(R_av, communs)
                Xap, _ = coords(R_ap, communs)
                t, _ = aligne_procruste(Xav, Xap)
                vals.append(deplacement(t(Xav), Xap))
            balayages["couverture"].append({"corpus": nom, "couverture": couv,
                                            "residus": vals,
                                            "median": float(np.median(vals))})

    original = m2.N_PTPT_NOUVEAUX
    for n_new in (50, 100, 200, 400):
        m2.N_PTPT_NOUVEAUX = n_new
        for nom in ("flou", "net"):
            vals = []
            for g in graines:
                v_av, v_ap, communs = m2.construis_corpus(g, CORPUS[nom])
                R_av = run_pipeline(votes=v_av, random_state=RANDOM_STATE)
                R_ap = run_pipeline(votes=v_ap, random_state=RANDOM_STATE)
                Xav, _ = coords(R_av, communs)
                Xap, _ = coords(R_ap, communs)
                t, _ = aligne_procruste(Xav, Xap)
                vals.append(deplacement(t(Xav), Xap))
            balayages["taille"].append({"corpus": nom, "nouveaux": n_new,
                                        "total": 50 + n_new, "residus": vals,
                                        "median": float(np.median(vals))})
    m2.N_PTPT_NOUVEAUX = original
    return balayages


if __name__ == "__main__":
    print(json.dumps({"A_donnees_reelles": partie_A(),
                      "B1_origine_du_residu": partie_B1(),
                      "B2_balayages": partie_B2()},
                     indent=2, ensure_ascii=False))
