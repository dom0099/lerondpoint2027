"""Analyse en deux étages : projection de red-dwarf, regroupement à nous.

`reddwarf.implementations.polis.run_pipeline` fait tout d'un bloc : réduction,
regroupement, statistiques. L'option C' du chantier D a besoin de s'intercaler **entre**
la réduction et le regroupement, pour calculer les groupes sur des seaux pondérés plutôt
que sur les participants bruts. On reprend donc l'enchaînement à notre compte.

DÉPENDANCE À RED-DWARF — à lire avant toute montée de version
=============================================================
Version épinglée : **red-dwarf 0.4.0** (`pyproject.toml` : `red-dwarf>=0.4`).

Les fonctions appelées ici sont importables et documentées :

    reddwarf.utils.matrix.generate_raw_matrix
    reddwarf.utils.matrix.simple_filter_matrix
    reddwarf.utils.matrix.get_clusterable_participant_ids
    reddwarf.utils.reducer.base.run_reducer
    reddwarf.utils.stats.calculate_comment_statistics_dataframes   (accepte nos étiquettes)
    reddwarf.utils.stats.populate_priority_calculations_into_statements_df
    reddwarf.utils.stats.select_representative_statements
    reddwarf.sklearn.cluster.PolisKMeans                            (via app/analysis/buckets.py)

**Ce qui n'est PAS une API publique, c'est leur ordonnancement** : la séquence ci-dessous
est calquée sur le corps de `reddwarf/implementations/base.py::run_pipeline` (0.4.0),
lignes ~100 à 200. Une montée de version peut la changer sans que rien ne casse à
l'import. Avant toute montée de version volontaire :

  1. relire `implementations/base.py` et comparer à `_projette` / `_statistiques` ici ;
  2. rejouer les bancs de mesure conservés dans `~/red-dwarf-test/d0_mesures/`
     (`mesures_d0.py`, `mesures_d0_suite2.py` … `banc_option_c_prime.py`) et vérifier
     que les ordres de grandeur du journal D0 sont retrouvés.

Écarts assumés par rapport à `run_pipeline` : le consensus global
(`select_consensus_statements`) et les projections de propositions exposées telles
quelles ne sont pas recalculés ici — rien dans la persistance du chantier C ne les
consomme aujourd'hui.
"""

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sklearn.decomposition import PCA
from sklearn.metrics import silhouette_score

from reddwarf.sklearn.cluster import PolisKMeans
from reddwarf.utils.matrix import (
    generate_raw_matrix,
    get_clusterable_participant_ids,
    simple_filter_matrix,
)
from reddwarf.utils.reducer.base import run_reducer
from reddwarf.utils.stats import (
    calculate_comment_statistics_dataframes,
    populate_priority_calculations_into_statements_df,
    select_representative_statements,
)

from app.analysis import buckets
from app.analysis.smoothing import EtatLisseur, lisse

#: Comme red-dwarf : k testé de 2 à 5.
K_MAX = 5


@dataclass
class Clusterer:
    """Le strict nécessaire pour que la persistance existante lise `n_clusters`."""

    n_clusters: int


@dataclass
class ResultatCouches:
    """Même surface que `PolisClusteringResult` pour ce que la persistance consomme."""

    participants_df: pd.DataFrame
    statements_df: pd.DataFrame
    repness: dict
    clusterer: Clusterer
    #: État à réécrire dans le cache : seau -> participants.
    appartenances: dict[int, set[int]] = field(default_factory=dict)
    #: Centre de chaque seau, pour l'audit (il se recalcule depuis les membres).
    centres: dict[int, Any] = field(default_factory=dict)
    #: Découpage groupe courant : étiquette brute -> participants.
    groupes: dict[int, set[int]] = field(default_factory=dict)
    #: Proposition -> votes exprimés DANS CHAQUE groupe (colonne `ns` de red-dwarf).
    #: Le clivage du L2 en a besoin pour deux raisons distinctes : la règle du plancher
    #: porte sur chaque groupe pris séparément, et le nombre de groupes donne l'exposant
    #: de la moyenne géométrique. Ce sont exactement les groupes entrés dans le produit
    #: du consensus — ni un de plus, ni un de moins.
    vues_par_groupe: dict[int, tuple[int, ...]] = field(default_factory=dict)
    etat_lisseur: EtatLisseur = field(default_factory=EtatLisseur)
    #: Pour le journal : ce que le lisseur a retenu, et ce qu'il a écarté.
    diagnostic: dict[str, Any] = field(default_factory=dict)


def _niveau_groupe(
    centres_seaux: np.ndarray,
    poids: np.ndarray,
    appartenances_ordonnees: list[set[int]],
    positions: dict[int, np.ndarray],
    centres_depart: np.ndarray | None,
    force_k: int | None,
    random_state: int | None,
) -> dict[int, tuple[float, PolisKMeans]]:
    """Ajuste un k-means par k candidat, sur les seaux pondérés.

    Les centres de départ de chaque candidat sont dérivés de l'unique couche vivante
    (`buckets.centres_pour_k`) : pas d'historique séparé par valeur de k.

    **La silhouette est calculée sur les participants, pas sur les centres de seaux.**
    Pol.is la calcule au niveau des buckets, mais il en a jusqu'à cent ; nous en avons
    parfois deux — et sur deux points la silhouette n'est même pas définie. Sur une
    conversation où tout le monde vote comme l'un ou l'autre camp, le regroupement
    retombait alors à un seul groupe, là où red-dwarf en trouvait deux : régression
    attrapée par `test_a_participant_sees_their_group_and_its_size`. Le k-means, lui,
    continue de tourner sur les seaux pondérés — c'est là qu'est l'amortissement.
    """
    n_seaux = len(centres_seaux)
    n_participants = len(positions)
    plafond = min(K_MAX, n_seaux, max(n_participants - 1, 0))
    if force_k is not None:
        candidats = [force_k] if 2 <= force_k <= plafond else []
    else:
        candidats = list(range(2, plafond + 1))

    ordre_participants = sorted(positions)
    X_participants = np.array([positions[p] for p in ordre_participants])
    seau_du_participant = {
        pid: index
        for index, membres in enumerate(appartenances_ordonnees)
        for pid in membres
    }

    modeles: dict[int, tuple[float, PolisKMeans]] = {}
    for k in candidats:
        depart = buckets.centres_pour_k(centres_depart, centres_seaux, k)
        modele = PolisKMeans(
            n_clusters=k, init="polis", init_centers=depart, random_state=random_state
        ).fit(centres_seaux, sample_weight=poids)
        etiquettes = [
            int(modele.labels_[seau_du_participant[p]]) for p in ordre_participants
        ]
        distinctes = len(set(etiquettes))
        if distinctes < 2 or distinctes >= len(etiquettes):
            continue
        modeles[k] = (float(silhouette_score(X_participants, etiquettes)), modele)
    return modeles


def analyse_par_couches(
    votes: list[dict],
    meta_statement_ids: list[int],
    mod_out_statement_ids: list[int],
    min_user_vote_threshold: int,
    force_group_count: int | None,
    random_state: int | None,
    appartenances_precedentes: dict[int, set[int]] | None,
    groupes_precedents: dict[int, set[int]] | None,
    etat_lisseur: EtatLisseur,
    tours_lisseur: int,
    pick_max: int = 5,
    confidence: float = 0.9,
) -> ResultatCouches:
    """Projection red-dwarf, puis seaux, puis groupes, puis statistiques red-dwarf."""
    # --- étage 1 : projection (identique à run_pipeline) --------------------------
    matrice_brute = generate_raw_matrix(votes=votes)
    matrice_filtree = simple_filter_matrix(
        vote_matrix=matrice_brute, mod_out_statement_ids=mod_out_statement_ids
    )
    X_participants, X_propositions, reducteur = run_reducer(
        vote_matrix=matrice_filtree.values, reducer="pca", random_state=random_state
    )
    participants_df = pd.DataFrame(
        X_participants, columns=pd.Index(["x", "y"]), index=matrice_filtree.index
    )
    a_regrouper = get_clusterable_participant_ids(
        matrice_brute, vote_threshold=min_user_vote_threshold
    )

    # --- étage 2 : notre couche intermédiaire, puis les groupes -------------------
    positions = {
        int(pid): participants_df.loc[pid, ["x", "y"]].to_numpy(dtype=float)
        for pid in a_regrouper
    }
    cible = buckets.taille_de_couche(len(positions))
    appartenances, centres = buckets.couche_de_base(
        positions, appartenances_precedentes, cible, random_state
    )
    ordre = sorted(centres)
    centres_seaux = np.array([centres[s] for s in ordre])
    poids = np.array([float(len(appartenances[s])) for s in ordre])

    # Le découpage groupe du tour précédent est lui aussi recentré sur les positions
    # actuelles de ses membres : rien n'est transporté d'un repère à l'autre.
    centres_depart = None
    if groupes_precedents:
        recentres, _ = buckets.recentre(groupes_precedents, positions)
        if recentres:
            centres_depart = np.array([recentres[g] for g in sorted(recentres)])

    modeles = _niveau_groupe(
        centres_seaux,
        poids,
        [appartenances[s] for s in ordre],
        positions,
        centres_depart,
        force_group_count,
        random_state,
    )

    if not modeles:
        # Trop peu de seaux distincts pour dégager le moindre groupe : tout le monde
        # dans un seul groupe plutôt qu'une exception.
        etiquettes_seaux = {s: 0 for s in ordre}
        k_retenu, diagnostic = 1, {"raison": "un seul groupe possible"}
        nouvel_etat = etat_lisseur
    else:
        candidat = max(modeles, key=lambda k: modeles[k][0])
        if force_group_count is not None:
            k_retenu = candidat
            nouvel_etat = EtatLisseur(dernier_k=candidat, compte=1, k_affiche=candidat)
            diagnostic = {"k_candidat": candidat, "lisseur": "court-circuité (k figé)"}
        else:
            nouvel_etat = lisse(etat_lisseur, candidat, set(modeles), tours_lisseur)
            k_retenu = nouvel_etat.k_affiche
            diagnostic = {
                "k_candidat": candidat,
                "k_affiche": k_retenu,
                "tours_consecutifs": nouvel_etat.compte,
                "silhouettes": {str(k): round(s, 4) for k, (s, _) in modeles.items()},
            }
        etiquettes_seaux = {
            s: int(e) for s, e in zip(ordre, modeles[k_retenu][1].labels_)
        }

    groupes: dict[int, set[int]] = {}
    etiquette_par_participant: dict[int, int] = {}
    for seau, etiquette in etiquettes_seaux.items():
        groupes.setdefault(etiquette, set()).update(appartenances[seau])
        for pid in appartenances[seau]:
            etiquette_par_participant[pid] = etiquette

    # --- étage 3 : statistiques red-dwarf, nourries par NOS étiquettes ------------
    etiquettes = [etiquette_par_participant[int(pid)] for pid in a_regrouper]
    participants_df["to_cluster"] = participants_df.index.isin(a_regrouper)
    participants_df["cluster_id"] = pd.Series(
        etiquettes, index=pd.Index(a_regrouper), dtype="Int64"
    )

    stats_par_groupe, gac_df = calculate_comment_statistics_dataframes(
        vote_matrix=matrice_brute.loc[a_regrouper, :], cluster_labels=etiquettes
    )

    propositions_df = pd.DataFrame(
        X_propositions, columns=pd.Index(["x", "y"]), index=matrice_filtree.columns
    )
    propositions_df["to_zero"] = propositions_df.index.isin(mod_out_statement_ids)
    propositions_df["is_meta"] = propositions_df.index.isin(meta_statement_ids)
    if isinstance(reducteur, PCA):
        composantes = list(reducteur.components_)
        propositions_df["mean"] = reducteur.mean_
        for rang in range(3):
            propositions_df[f"pc{rang + 1}"] = (
                composantes[rang] if rang < len(composantes) else None
            )
        propositions_df = populate_priority_calculations_into_statements_df(
            statements_df=propositions_df,
            vote_matrix=matrice_brute.loc[a_regrouper, :],
        )
    propositions_df = pd.concat([propositions_df, gac_df], axis=1)

    # Les votes exprimés par groupe, pour la règle du plancher du L2. `unstack` remet
    # les groupes en colonnes ; l'ordre des colonnes est sans importance — seuls leur
    # nombre et leur minimum servent.
    vues_par_groupe = {
        int(statement_id): tuple(int(v) for v in ligne)
        for statement_id, ligne in stats_par_groupe["ns"].unstack("group_id").iterrows()
    }

    repness = select_representative_statements(
        grouped_stats_df=stats_par_groupe,
        mod_out_statement_ids=mod_out_statement_ids,
        pick_max=pick_max,
        confidence=confidence,
    )

    return ResultatCouches(
        participants_df=participants_df,
        statements_df=propositions_df,
        repness=repness,
        clusterer=Clusterer(n_clusters=len(groupes)),
        appartenances=appartenances,
        centres=centres,
        groupes=groupes,
        vues_par_groupe=vues_par_groupe,
        etat_lisseur=nouvel_etat,
        diagnostic={**diagnostic, "seaux": len(appartenances), "cible_seaux": cible},
    )
