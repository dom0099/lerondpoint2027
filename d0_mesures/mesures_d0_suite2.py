"""Chantier D / D0 (suite 2) — généralisation des mesures.

  1. alignement de Procrustes/Kabsch (rotation + réflexion, SVD) à la place des flips ;
  2. mesure 3 rejouée avec cet alignement automatique ;
  3. second corpus « flou » (groupes moins séparés, bruit plus élevé) ;
  4. plusieurs graines, mêmes tailles 50 -> 150.

Lecture/expérimentation seule. Aucune donnée réelle.
"""

import json
import numpy as np
from scipy.optimize import linear_sum_assignment

from reddwarf.implementations.polis import run_pipeline

RANDOM_STATE = 42
GRAINES = [20260903, 7, 1234, 99991]
N_GROUPES = 4
N_DECL_AVANT = 30
N_DECL_NOUVELLES = 6
N_PTPT_AVANT = 50
N_PTPT_NOUVEAUX = 100

# Deux régimes : le corpus « net » du premier essai, et un corpus délibérément flou.
CORPUS = {
    "net":  {"p_haut": 0.85, "p_bas": 0.15, "passe": 0.08, "couverture": 0.90},
    "flou": {"p_haut": 0.62, "p_bas": 0.38, "passe": 0.18, "couverture": 0.70},
}


# --------------------------------------------------------------------------- corpus
def fabrique_votes(rng, participants, declarations, profils, reglages):
    votes, horodatage = [], 1788000000000
    for pid, groupe in participants:
        for sid in declarations:
            if rng.random() > reglages["couverture"]:
                continue
            horodatage += 1000
            if rng.random() < reglages["passe"]:
                v = 0
            else:
                v = 1 if rng.random() < profils[groupe][sid] else -1
            votes.append({"participant_id": pid, "statement_id": sid,
                          "vote": v, "modified": horodatage})
    return votes


def construis_corpus(graine, reglages):
    rng = np.random.default_rng(graine)
    toutes = list(range(N_DECL_AVANT + N_DECL_NOUVELLES))
    ph, pb = reglages["p_haut"], reglages["p_bas"]
    profils = {g: {} for g in range(N_GROUPES)}
    for sid in toutes:
        if sid % 5 == 0:
            for g in range(N_GROUPES):
                profils[g][sid] = ph
        else:
            for g in range(N_GROUPES):
                profils[g][sid] = ph if (sid + g) % 2 == 0 else pb
            if sid % 3 == 0:
                for g in range(N_GROUPES):
                    profils[g][sid] = ph if g < 2 else pb

    anciens = [(pid, pid % N_GROUPES) for pid in range(N_PTPT_AVANT)]
    nouveaux = [(pid, pid % N_GROUPES)
                for pid in range(N_PTPT_AVANT, N_PTPT_AVANT + N_PTPT_NOUVEAUX)]
    v_anciens = fabrique_votes(rng, anciens, list(range(N_DECL_AVANT)), profils, reglages)
    v_nouveaux = fabrique_votes(rng, nouveaux, toutes, profils, reglages)
    return v_anciens, v_anciens + v_nouveaux, [pid for pid, _ in anciens]


# ------------------------------------------------------------------- alignement
def aligne_procruste(A, B, avec_echelle=True):
    """Meilleure transformation orthogonale (rotation OU réflexion) de A vers B.

    Kabsch/Procruste par SVD : on ne suppose rien du signe des axes, on le déduit.
    Renvoie la fonction de transport, applicable à n'importe quel point du repère A
    (participants comme centres de groupes).
    """
    mA, mB = A.mean(axis=0), B.mean(axis=0)
    Ac, Bc = A - mA, B - mB
    U, S, Vt = np.linalg.svd(Ac.T @ Bc)
    R = U @ Vt                      # det(R) libre : réflexion autorisée
    s = S.sum() / (Ac ** 2).sum() if avec_echelle else 1.0

    def transporte(P):
        return s * ((np.asarray(P, dtype=float) - mA) @ R) + mB

    return transporte, {
        "reflexion": bool(np.linalg.det(R) < 0),
        "echelle": float(s),
        "axe_ou_angle_deg": float(np.degrees(np.arctan2(R[1, 0], R[0, 0]))),
    }


# ------------------------------------------------------------------- utilitaires
def coords(res, pids=None):
    df = res.participants_df
    if pids is not None:
        df = df.loc[df.index.intersection(pids)]
    return df[["x", "y"]].to_numpy(), list(df.index)


def partition(res):
    df = res.participants_df
    df = df[df["cluster_id"].notna()]
    g = {}
    for pid, cid in zip(df.index, df["cluster_id"]):
        g.setdefault(int(cid), set()).add(int(pid))
    return g


def jaccard_apparie(p1, p2, univers=None):
    if univers is not None:
        p1 = {k: v & univers for k, v in p1.items()}
        p2 = {k: v & univers for k, v in p2.items()}
        p1 = {k: v for k, v in p1.items() if v}
        p2 = {k: v for k, v in p2.items() if v}
    k1, k2 = sorted(p1), sorted(p2)
    M = np.zeros((len(k1), len(k2)))
    for i, a in enumerate(k1):
        for j, b in enumerate(k2):
            u = len(p1[a] | p2[b])
            M[i, j] = len(p1[a] & p2[b]) / u if u else 0.0
    li, co = linear_sum_assignment(-M)
    paires = [(k1[i], k2[j], float(M[i, j]), len(p1[k1[i]])) for i, j in zip(li, co)]
    poids = sum(n for *_, n in paires)
    moy = sum(j * n for *_, j, n in paires) / poids if poids else 0.0
    return paires, float(moy)


def deplacement(A, B):
    d = np.linalg.norm(A - B, axis=1)
    rayon = np.sqrt((np.linalg.norm(B - B.mean(axis=0), axis=1) ** 2).mean())
    return round(float(100 * d.mean() / rayon), 1)


def flip(A, fx, fy):
    X = A.copy()
    if fx:
        X[:, 0] *= -1
    if fy:
        X[:, 1] *= -1
    return X


# ------------------------------------------------------------------------ essai
def un_essai(nom_corpus, graine):
    reglages = CORPUS[nom_corpus]
    v_avant, v_apres, communs = construis_corpus(graine, reglages)

    r_avant = run_pipeline(votes=v_avant, random_state=RANDOM_STATE)
    r_froid = run_pipeline(votes=v_apres, random_state=RANDOM_STATE)
    # témoin de déterminisme, refait sur chaque corpus
    r_bis = run_pipeline(votes=v_apres, random_state=RANDOM_STATE)
    Cc, _ = coords(r_froid)
    Cb, _ = coords(r_bis)

    Xav, ids_av = coords(r_avant, communs)
    Xap, ids_ap = coords(r_froid, communs)
    assert ids_av == ids_ap

    transporte, info = aligne_procruste(Xav, Xap)
    res = {
        "corpus": nom_corpus,
        "graine": graine,
        "n_communs": len(ids_av),
        "temoin_bit_a_bit": bool(np.array_equal(Cc, Cb)),
        "k_avant": len(partition(r_avant)),
        "k_apres_froid": len(partition(r_froid)),
        "deplacement_%_rayon": {
            "sans correction": deplacement(Xav, Xap),
            "flip_x": deplacement(flip(Xav, True, False), Xap),
            "flip_y": deplacement(flip(Xav, False, True), Xap),
            "flip_x+flip_y (defaut biblio)": deplacement(flip(Xav, True, True), Xap),
            "ALIGNEMENT AUTO (Procruste)": deplacement(transporte(Xav), Xap),
        },
        "alignement": {k: (round(v, 3) if isinstance(v, float) else v)
                       for k, v in info.items()},
    }

    # ---- mesure 3 : centres d'avant transportés automatiquement dans le repère d'après
    centres_avant = r_avant.clusterer.cluster_centers_
    centres_auto = transporte(centres_avant).tolist()
    centres_flipxy = flip(centres_avant.copy(), True, True).tolist()

    r_chaud_auto = run_pipeline(votes=v_apres, random_state=RANDOM_STATE,
                                init_centers=centres_auto)
    r_chaud_flipxy = run_pipeline(votes=v_apres, random_state=RANDOM_STATE,
                                  init_centers=centres_flipxy)

    p_av, p_fr = partition(r_avant), partition(r_froid)
    p_auto, p_fxy = partition(r_chaud_auto), partition(r_chaud_flipxy)
    U = set(communs)

    def identite(pa, pb):
        paires, _ = jaccard_apparie(pa, pb, U)
        return {"appariement": {str(a): int(b) for a, b, _, _ in paires},
                "preservee": all(a == b for a, b, _, _ in paires)}

    res["mesure_3"] = {
        "k_chaud_auto": len(p_auto),
        "k_chaud_flipxy": len(p_fxy),
        "jaccard_froid_vs_chaud_auto": round(jaccard_apparie(p_fr, p_auto)[1], 4),
        "jaccard_froid_vs_chaud_flipxy": round(jaccard_apparie(p_fr, p_fxy)[1], 4),
        "jaccard_avant_vs_froid_communs": round(jaccard_apparie(p_av, p_fr, U)[1], 4),
        "jaccard_avant_vs_chaud_auto_communs": round(jaccard_apparie(p_av, p_auto, U)[1], 4),
        "identite_froid": identite(p_av, p_fr),
        "identite_chaud_auto": identite(p_av, p_auto),
        "identite_chaud_flipxy": identite(p_av, p_fxy),
    }
    return res


def main():
    tout = []
    for nom in CORPUS:
        for graine in GRAINES:
            tout.append(un_essai(nom, graine))
    print(json.dumps(tout, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
