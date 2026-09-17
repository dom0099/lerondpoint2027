"""Chantier D / D0 — mesures empiriques sur red-dwarf 0.4.0.

Trois mesures, en lecture/expérimentation seule :
  1. le repère est-il stable à données stricitement identiques ?
  2. de combien dérive-t-il quand les données grossissent ?
  3. `init_centers` change-t-il réellement le découpage obtenu ?

Aucune donnée réelle : corpus synthétique à graine fixe, généré ici.
"""

import json
import numpy as np
from scipy.optimize import linear_sum_assignment

from reddwarf.implementations.polis import run_pipeline
from reddwarf.utils.polismath import get_corrected_centroid_guesses

GRAINE = 20260903
RANDOM_STATE = 42
N_GROUPES = 4
N_DECL_AVANT = 30
N_DECL_NOUVELLES = 6
N_PTPT_AVANT = 50
N_PTPT_NOUVEAUX = 100


def fabrique_votes(rng, participants, declarations, profils, couverture=0.9, passe=0.08):
    """Vote synthétique : chaque participant suit le profil d'accord de son groupe."""
    votes = []
    horodatage = 1788000000000
    for pid, groupe in participants:
        for sid in declarations:
            if rng.random() > couverture:
                continue
            horodatage += 1000
            if rng.random() < passe:
                v = 0
            else:
                v = 1 if rng.random() < profils[groupe][sid] else -1
            votes.append({
                "participant_id": pid,
                "statement_id": sid,
                "vote": v,
                "modified": horodatage,
            })
    return votes


def construis_corpus():
    rng = np.random.default_rng(GRAINE)
    toutes_decl = list(range(N_DECL_AVANT + N_DECL_NOUVELLES))
    # Profils d'accord par groupe : 6 déclarations de consensus, le reste clivant.
    profils = {g: {} for g in range(N_GROUPES)}
    for sid in toutes_decl:
        if sid % 5 == 0:  # consensus
            p = 0.85
            for g in range(N_GROUPES):
                profils[g][sid] = p
        else:
            for g in range(N_GROUPES):
                profils[g][sid] = 0.85 if (sid + g) % 2 == 0 else 0.15
            # une déclaration sur trois oppose les groupes deux à deux
            if sid % 3 == 0:
                for g in range(N_GROUPES):
                    profils[g][sid] = 0.85 if g < 2 else 0.15

    anciens = [(pid, pid % N_GROUPES) for pid in range(N_PTPT_AVANT)]
    nouveaux = [(pid, pid % N_GROUPES)
                for pid in range(N_PTPT_AVANT, N_PTPT_AVANT + N_PTPT_NOUVEAUX)]

    decl_avant = list(range(N_DECL_AVANT))
    # Les anciens participants ont exactement les mêmes votes dans les deux jeux.
    votes_anciens = fabrique_votes(rng, anciens, decl_avant, profils)
    votes_nouveaux = fabrique_votes(rng, nouveaux, toutes_decl, profils)

    return votes_anciens, votes_anciens + votes_nouveaux, [pid for pid, _ in anciens]


def coords(res, pids=None):
    df = res.participants_df
    if pids is not None:
        df = df.loc[df.index.intersection(pids)]
    return df[["x", "y"]].to_numpy(), list(df.index)


def partition(res):
    df = res.participants_df
    df = df[df["cluster_id"].notna()]
    groupes = {}
    for pid, cid in zip(df.index, df["cluster_id"]):
        groupes.setdefault(int(cid), set()).add(int(pid))
    return groupes


def jaccard_apparie(p1, p2, univers=None):
    """Jaccard entre deux découpages, groupes appariés au mieux (hongrois)."""
    if univers is not None:
        p1 = {k: (v & univers) for k, v in p1.items()}
        p2 = {k: (v & univers) for k, v in p2.items()}
        p1 = {k: v for k, v in p1.items() if v}
        p2 = {k: v for k, v in p2.items() if v}
    k1, k2 = sorted(p1), sorted(p2)
    M = np.zeros((len(k1), len(k2)))
    for i, a in enumerate(k1):
        for j, b in enumerate(k2):
            inter = len(p1[a] & p2[b])
            union = len(p1[a] | p2[b])
            M[i, j] = inter / union if union else 0.0
    li, co = linear_sum_assignment(-M)
    paires = [(k1[i], k2[j], M[i, j], len(p1[k1[i]])) for i, j in zip(li, co)]
    poids = sum(n for *_, n in paires)
    moyenne_ponderee = sum(j * n for *_, j, n in paires) / poids if poids else 0.0
    return M, k1, k2, paires, moyenne_ponderee


def stats_deplacement(A, B):
    d = np.linalg.norm(A - B, axis=1)
    rayon = np.sqrt((np.linalg.norm(B - B.mean(axis=0), axis=1) ** 2).mean())
    return {
        "moyenne": float(d.mean()),
        "mediane": float(np.median(d)),
        "p95": float(np.percentile(d, 95)),
        "max": float(d.max()),
        "rayon_nuage_apres": float(rayon),
        "moyenne_relative_%": float(100 * d.mean() / rayon),
    }


def alignement_optimal(A, B):
    """Kabsch avec échelle : meilleure similitude possible de A vers B."""
    Ac, Bc = A - A.mean(axis=0), B - B.mean(axis=0)
    U, S, Vt = np.linalg.svd(Ac.T @ Bc)
    R = U @ Vt
    reflexion = bool(np.linalg.det(R) < 0)
    echelle = S.sum() / (Ac ** 2).sum()
    A_align = echelle * (Ac @ R)
    residu = np.linalg.norm(A_align - Bc, axis=1)
    rayon = np.sqrt((np.linalg.norm(Bc, axis=1) ** 2).mean())
    angle = float(np.degrees(np.arctan2(R[1, 0], R[0, 0])))
    return {
        "angle_deg": angle,
        "reflexion": reflexion,
        "echelle": float(echelle),
        "residu_moyen": float(residu.mean()),
        "residu_moyen_relatif_%": float(100 * residu.mean() / rayon),
    }


def main():
    sortie = {}
    votes_avant, votes_apres, pids_communs = construis_corpus()
    sortie["corpus"] = {
        "votes_avant": len(votes_avant),
        "votes_apres": len(votes_apres),
        "ptpt_avant": len({v["participant_id"] for v in votes_avant}),
        "ptpt_apres": len({v["participant_id"] for v in votes_apres}),
        "decl_avant": len({v["statement_id"] for v in votes_avant}),
        "decl_apres": len({v["statement_id"] for v in votes_apres}),
        "graine": GRAINE,
        "random_state": RANDOM_STATE,
    }

    # ---------- Mesure 1 : témoin de contrôle ----------
    r1a = run_pipeline(votes=votes_avant, random_state=RANDOM_STATE)
    r1b = run_pipeline(votes=votes_avant, random_state=RANDOM_STATE)
    A, ids_a = coords(r1a)
    B, ids_b = coords(r1b)
    p1a, p1b = partition(r1a), partition(r1b)
    sortie["mesure_1"] = {
        "memes_participants": ids_a == ids_b,
        "identique_bit_a_bit": bool(np.array_equal(A, B)),
        "ecart_max_absolu": float(np.abs(A - B).max()),
        "k_run_a": len(p1a),
        "k_run_b": len(p1b),
        "partitions_identiques": p1a == p1b,
    }
    # même contrôle sur le jeu « après »
    r1c = run_pipeline(votes=votes_apres, random_state=RANDOM_STATE)
    r1d = run_pipeline(votes=votes_apres, random_state=RANDOM_STATE)
    C, _ = coords(r1c)
    D, _ = coords(r1d)
    sortie["mesure_1"]["apres_identique_bit_a_bit"] = bool(np.array_equal(C, D))
    sortie["mesure_1"]["apres_ecart_max_absolu"] = float(np.abs(C - D).max())

    # ---------- Mesure 2 : dérive du repère ----------
    res_avant, res_apres = r1a, r1c
    Xav, ids_av = coords(res_avant, pids_communs)
    Xap, ids_ap = coords(res_apres, pids_communs)
    assert ids_av == ids_ap, "participants communs désalignés"
    sortie["mesure_2"] = {
        "n_participants_communs": len(ids_av),
        "k_avant": len(partition(res_avant)),
        "k_apres": len(partition(res_apres)),
        "variantes": {},
    }
    for nom, (fx, fy) in {
        "sans correction": (False, False),
        "flip_x": (True, False),
        "flip_y": (False, True),
        "flip_x+flip_y (défaut de get_corrected_centroid_guesses)": (True, True),
    }.items():
        X = Xav.copy()
        if fx:
            X[:, 0] *= -1
        if fy:
            X[:, 1] *= -1
        sortie["mesure_2"]["variantes"][nom] = stats_deplacement(X, Xap)
    sortie["mesure_2"]["alignement_optimal"] = alignement_optimal(Xav, Xap)

    # ---------- Mesure 3 : init_centers change-t-il quelque chose ? ----------
    centres_avant = res_avant.clusterer.cluster_centers_.tolist()
    faux_polismath = {"group-clusters": [{"center": c} for c in centres_avant]}
    guesses_flip = get_corrected_centroid_guesses(faux_polismath, source="group-clusters",
                                                  flip_x=True, flip_y=True)
    guesses_brut = get_corrected_centroid_guesses(faux_polismath, source="group-clusters",
                                                  flip_x=False, flip_y=False)
    # Variante imposée par la mesure 2 : sur ces données, c'est flip_x SEUL qui aligne.
    guesses_flipx = get_corrected_centroid_guesses(faux_polismath, source="group-clusters",
                                                   flip_x=True, flip_y=False)

    froid = res_apres
    chaud_flip = run_pipeline(votes=votes_apres, random_state=RANDOM_STATE,
                              init_centers=guesses_flip)
    chaud_brut = run_pipeline(votes=votes_apres, random_state=RANDOM_STATE,
                              init_centers=guesses_brut)
    chaud_flipx = run_pipeline(votes=votes_apres, random_state=RANDOM_STATE,
                               init_centers=guesses_flipx)

    p_froid, p_chaud_f, p_chaud_b = partition(froid), partition(chaud_flip), partition(chaud_brut)
    p_chaud_x = partition(chaud_flipx)
    p_avant = partition(res_avant)
    communs = set(pids_communs)

    def bloc(nom, pa, pb, univers=None):
        M, k1, k2, paires, moy = jaccard_apparie(pa, pb, univers)
        return {
            "comparaison": nom,
            "k_gauche": len(k1),
            "k_droite": len(k2),
            "jaccard_moyen_pondere": round(moy, 4),
            "paires": [{"groupe_gauche": a, "groupe_droit": b,
                        "jaccard": round(j, 4), "taille_gauche": n}
                       for a, b, j, n in paires],
        }

    def identites_conservees(pa, pb):
        """Le groupe i d'avant redevient-il le groupe i après ? (continuité des étiquettes)"""
        _, _, _, paires, _ = jaccard_apparie(pa, pb, communs)
        return {"appariement": {str(a): int(b) for a, b, _, _ in paires},
                "identite_preservee": all(a == b for a, b, _, _ in paires)}

    sortie["mesure_3"] = {
        "centres_avant": [[round(v, 6) for v in c] for c in centres_avant],
        "centres_transmis_flip": [[round(v, 6) for v in c] for c in guesses_flip],
        "k_froid": len(p_froid),
        "k_chaud_flip": len(p_chaud_f),
        "k_chaud_brut": len(p_chaud_b),
        "k_chaud_flipx": len(p_chaud_x),
        "correspondance_identifiants": {
            "froid": identites_conservees(p_avant, p_froid),
            "chaud_flip(defaut)": identites_conservees(p_avant, p_chaud_f),
            "chaud_flipx": identites_conservees(p_avant, p_chaud_x),
        },
        "init_centers_reellement_utilises_flip":
            [[round(v, 6) for v in c] for c in chaud_flip.clusterer.init_centers_used_.tolist()],
        "labels_froid_vs_chaud_flip_identiques":
            list(froid.participants_df["cluster_id"]) == list(chaud_flip.participants_df["cluster_id"]),
        "jaccard": [
            bloc("froid(après) vs chaud-flip(après) — tous participants", p_froid, p_chaud_f),
            bloc("froid(après) vs chaud-brut(après) — tous participants", p_froid, p_chaud_b),
            bloc("avant vs froid(après) — sur les 50 communs", p_avant, p_froid, communs),
            bloc("avant vs chaud-flip(après) — sur les 50 communs", p_avant, p_chaud_f, communs),
            bloc("avant vs chaud-brut(après) — sur les 50 communs", p_avant, p_chaud_b, communs),
            bloc("froid(après) vs chaud-flipx(après) — tous participants", p_froid, p_chaud_x),
            bloc("avant vs chaud-flipx(après) — sur les 50 communs", p_avant, p_chaud_x, communs),
        ],
    }

    print(json.dumps(sortie, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
