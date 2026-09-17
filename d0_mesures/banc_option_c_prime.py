"""Chantier D / D1 — banc de VÉRIFICATION de l'option C' (pas une implémentation).

Question à trancher par la mesure, pas par le raisonnement :
    le recentrage de la couche intermédiaire sur les membres actuels suffit-il à
    absorber la rotation du plan PCA, ou faut-il en plus un réalignement de Procruste ?

Trois variantes du niveau groupe sont comparées sur le jeu « après » :
    froid        : init="polis", aucun état transmis
    C' (membres) : centres du tour précédent RECALCULÉS à partir des coordonnées
                   ACTUELLES de leurs membres — on transporte l'appartenance, pas
                   les coordonnées
    B (transport): centres du tour précédent transportés par Procruste

N'utilise que des briques publiques de red-dwarf (run_pipeline, PolisKMeans) et numpy.
"""
import json, sys
import numpy as np
from sklearn.metrics import silhouette_score

from reddwarf.implementations.polis import run_pipeline
from reddwarf.sklearn.cluster import PolisKMeans

sys.path.insert(0, "/home/ubuntu/red-dwarf-test/d0_mesures")
import mesures_d0_suite2 as m2
from mesures_d0_suite2 import aligne_procruste, jaccard_apparie

RS = 42
BASE_K = 25          # couche intermédiaire : ~2 participants/seau « avant », ~6 « après »
K_MAX = 5
GRAINES = (20260903, 7, 1234, 99991)
NIVEAUX = {"net": (0.85, 0.08, 0.90), "flou": (0.62, 0.18, 0.70)}


def xy(res):
    df = res.participants_df
    return {int(p): np.array([r.x, r.y]) for p, r in df[["x", "y"]].iterrows()}


# --------------------------------------------------------- couche intermédiaire
def couche_base(coords, init_centers=None):
    """base-clusters. Sans init_centers : à froid. Avec : réamorcée.

    PolisKMeans complète lui-même les centres manquants par sa stratégie 'polis'
    (les premières lignes distinctes) : c'est exactement le rôle que joue la boucle
    de scission de `clean-start-clusters` chez Pol.is.
    """
    pids = sorted(coords)
    X = np.array([coords[p] for p in pids])
    k = min(BASE_K, len(np.unique(X, axis=0)))
    km = PolisKMeans(n_clusters=k, init="polis", init_centers=init_centers,
                     random_state=RS).fit(X)
    membres = {}
    for p, lab in zip(pids, km.labels_):
        membres.setdefault(int(lab), set()).add(p)
    return membres, km.cluster_centers_


def recentre(membres_precedents, coords_actuelles):
    """Le coeur de C' : un centre = moyenne des positions ACTUELLES de ses membres.

    Rien n'est transporté d'un repère à l'autre : les identifiants de membres sont
    relus dans le nouveau repère. Les seaux vidés disparaissent.
    """
    centres = []
    for bid in sorted(membres_precedents):
        pts = [coords_actuelles[p] for p in membres_precedents[bid] if p in coords_actuelles]
        if pts:
            centres.append(np.mean(pts, axis=0))
    return np.array(centres) if centres else None


# ------------------------------------------------------------- niveau groupe
def niveau_groupe(centres_base, poids, init_centers=None):
    """k choisi par silhouette sur les centres de base, pondérés par leurs effectifs."""
    best = None
    for k in range(2, min(K_MAX, len(centres_base) - 1) + 1):
        km = PolisKMeans(n_clusters=k, init="polis", init_centers=init_centers,
                         random_state=RS).fit(centres_base, sample_weight=poids)
        if len(set(km.labels_)) < 2:
            continue
        s = silhouette_score(centres_base, km.labels_)
        if best is None or s > best[0]:
            best = (s, k, km)
    return best  # (silhouette, k, modele)


def participants_par_groupe(membres_base, labels_base, ordre_bids):
    out = {}
    for bid, lab in zip(ordre_bids, labels_base):
        out.setdefault(int(lab), set()).update(membres_base[bid])
    return out


def essai(nom, graine):
    ph, pa, co = NIVEAUX[nom]
    reg = {"p_haut": ph, "p_bas": round(1 - ph, 2), "passe": pa, "couverture": co}
    v_av, v_ap, communs = m2.construis_corpus(graine, reg)

    r_av = run_pipeline(votes=v_av, random_state=RS)
    r_ap = run_pipeline(votes=v_ap, random_state=RS)
    A, B = xy(r_av), xy(r_ap)

    # --- tour 1 (à froid, comme le tout premier calcul d'une conversation) -------
    mb_av, c_av = couche_base(A)
    bids_av = sorted(mb_av)
    poids_av = np.array([len(mb_av[b]) for b in bids_av], dtype=float)
    sil_av, k_av, km_av = niveau_groupe(np.array([c_av[b] for b in bids_av]), poids_av)
    grp_av = participants_par_groupe(mb_av, km_av.labels_, bids_av)

    # --- tour 2, trois variantes ------------------------------------------------
    resultats = {}

    # (a) froid intégral
    mb_f, c_f = couche_base(B)
    bids_f = sorted(mb_f)
    P_f = np.array([len(mb_f[b]) for b in bids_f], dtype=float)
    sil_f, k_f, km_f = niveau_groupe(np.array([c_f[b] for b in bids_f]), P_f)
    resultats["froid"] = (participants_par_groupe(mb_f, km_f.labels_, bids_f), k_f)

    # (b) C' : couche recentrée sur les membres, groupe recentré sur les membres
    centres_recentres = recentre(mb_av, B)
    mb_c, c_c = couche_base(B, init_centers=centres_recentres)
    bids_c = sorted(mb_c)
    P_c = np.array([len(mb_c[b]) for b in bids_c], dtype=float)
    C_c = np.array([c_c[b] for b in bids_c])
    init_grp_membres = recentre(grp_av, B)     # <- aucune coordonnée transportée
    sil_c, k_c, km_c = niveau_groupe(C_c, P_c, init_centers=init_grp_membres)
    resultats["C_prime_membres"] = (participants_par_groupe(mb_c, km_c.labels_, bids_c), k_c)

    # (c) B : centres transportés par Procruste (coordonnées, pas appartenance)
    ids = [p for p in communs if p in A and p in B]
    t, info = aligne_procruste(np.array([A[p] for p in ids]), np.array([B[p] for p in ids]))
    centres_transportes = t(np.array([c_av[b] for b in bids_av]))
    mb_b, c_b = couche_base(B, init_centers=centres_transportes)
    bids_b = sorted(mb_b)
    P_b = np.array([len(mb_b[b]) for b in bids_b], dtype=float)
    init_grp_transport = t(km_av.cluster_centers_)
    sil_b, k_b, km_b = niveau_groupe(np.array([c_b[b] for b in bids_b]), P_b,
                                     init_centers=init_grp_transport)
    resultats["B_transport"] = (participants_par_groupe(mb_b, km_b.labels_, bids_b), k_b)

    U = set(communs)
    sortie = {"corpus": nom, "graine": graine, "k_avant": k_av,
              "alignement": {k: (round(v, 3) if isinstance(v, float) else v)
                             for k, v in info.items()}}
    for cle, (grp, k) in resultats.items():
        paires, J = jaccard_apparie(grp_av, grp, U)
        conserves = sum(1 for a, b, _, _ in paires if a == b)
        sortie[cle] = {"k": k, "jaccard_vs_avant": round(J, 4),
                       "identites_conservees": f"{conserves}/{len(paires)}"}
    # les deux amorçages donnent-ils le même découpage ?
    sortie["J_Cprime_vs_B"] = round(
        jaccard_apparie(resultats["C_prime_membres"][0], resultats["B_transport"][0])[1], 4)
    sortie["J_Cprime_vs_froid"] = round(
        jaccard_apparie(resultats["C_prime_membres"][0], resultats["froid"][0])[1], 4)
    return sortie


if __name__ == "__main__":
    print(json.dumps([essai(n, g) for n in NIVEAUX for g in GRAINES],
                     indent=2, ensure_ascii=False))
