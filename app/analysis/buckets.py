"""Couche intermédiaire de « seaux » (base-clusters), option C' du chantier D.

Les groupes d'opinion ne sont plus calculés sur les participants bruts mais sur une
centaine (au plus) de micro-groupes pondérés, réamorcés d'un recalcul à l'autre. C'est
le mécanisme de `clean-start-clusters` du moteur Clojure de Pol.is, lu au D0 et
transposé ici.

**Ce qui est transporté d'un tour à l'autre, ce sont les APPARTENANCES, jamais les
coordonnées.** Un centre est toujours recalculé à partir des positions actuelles de ses
membres, donc il est d'emblée exprimé dans le repère du nouveau calcul. C'est ce qui
rend inutile un réalignement de type Procruste : mesuré au D1 (banc
`d0_mesures/banc_option_c_prime.py`), le recentrage seul absorbe une rotation du plan
PCA, y compris une réflexion — C' et un transport par Procruste donnent alors le même
découpage (Jaccard 0,987 à 1,000 entre eux).

Trois opérations, dans cet ordre, à chaque recalcul :

  1. **recentrage** — chaque seau reprend pour centre la moyenne des positions actuelles
     de ses membres ; un seau dont tous les membres ont disparu est supprimé ;
  2. **fusion** — deux seaux dont les centres se sont rejoints n'en font plus qu'un ;
  3. **scission** — tant qu'on n'a pas atteint le nombre de seaux visé, on détache le
     point le plus éloigné de tout centre et on en fait un seau neuf.

Aucune dépendance à l'état interne de red-dwarf ici : uniquement `PolisKMeans`, dont le
paramètre `init_centers` est documenté.
"""

import math

import numpy as np
from reddwarf.sklearn.cluster import PolisKMeans

#: Plafond, comme le `base-k` de Pol.is. Au-delà, la couche ne gagne plus rien et le
#: coût de la boucle de scission devient sensible.
BASE_K_MAX = 100
#: Plancher du nombre de SEAUX dès que la couche cesse d'être neutre — et non
#: plancher du nombre de participants. La distinction a coûté un défaut : écrit
#: `max(BASE_K_MIN, ceil(n / 4))` avec un plancher à 20, la règle des quatre
#: participants par seau ne s'appliquait jamais avant 80 participants, et une
#: conversation de 24 personnes réclamait 20 seaux. Le k-means n'en soutenait que 11 :
#: les 9 autres naissaient et mouraient à chaque tour, en consommant des identifiants
#: neufs. Diagnostiqué sur base migrée le 4 septembre 2026.
#:
#: 10 seaux suffisent à exprimer jusqu'à `staged.K_MAX` = 5 groupes avec de la marge,
#: et c'est le point où la règle des quatre participants prend le relais sans à-coup :
#: 10 participants -> 10 seaux, 11 -> 10, 44 -> 11. Aucune discontinuité.
BASE_K_MIN = 10
#: Participants par seau visés. 4 amortit le mouvement individuel tout en gardant
#: assez de points pour le niveau groupe.
PARTICIPANTS_PAR_SEAU = 4

#: Deux centres plus proches que ce multiple du rayon du nuage sont fusionnés.
#: Pol.is ne fusionne que des centres STRICTEMENT identiques (`uniqify-clusters`) ;
#: on garde un epsilon relatif pour absorber le bruit du calcul flottant, et RIEN de
#: plus. Une tolérance large (1 % du rayon a été essayé) réunit des voisins pourtant
#: distincts, que la scission re-sépare aussitôt sous une identité neuve : un
#: participant sur dix changeait de seau à données inchangées. Du churn sans
#: contrepartie — c'est exactement ce que la couche existe pour éviter.
TOLERANCE_FUSION = 1e-9


def taille_de_couche(n_participants: int) -> int:
    """Nombre de seaux visé pour une conversation de `n_participants`.

    En dessous de `BASE_K_MIN` participants, il y a autant de seaux que de
    participants : la couche est alors une traversée neutre, ce qui est exactement le
    comportement de Pol.is sur une petite conversation (`base-k` = 100 pour 30
    participants revient au même).

    C'est une **cible**, pas une contrainte : elle dit jusqu'où scinder quand la couche
    est trop grossière. Elle ne force jamais une couche déjà plus fine à se réduire —
    un seau ne disparaît que vidé de ses membres ou fusionné avec son jumeau.
    """
    if n_participants <= BASE_K_MIN:
        return max(1, n_participants)
    return min(
        BASE_K_MAX,
        max(BASE_K_MIN, math.ceil(n_participants / PARTICIPANTS_PAR_SEAU)),
    )


def _rayon(positions: np.ndarray) -> float:
    """Rayon quadratique moyen du nuage — l'échelle à laquelle comparer des distances."""
    if len(positions) == 0:
        return 1.0
    ecarts = positions - positions.mean(axis=0)
    rayon = float(np.sqrt((np.linalg.norm(ecarts, axis=1) ** 2).mean()))
    return rayon if rayon > 0 else 1.0


def recentre(
    appartenances: dict[int, set[int]], positions: dict[int, np.ndarray]
) -> tuple[dict[int, np.ndarray], dict[int, set[int]]]:
    """Recalcule chaque centre depuis les positions ACTUELLES de ses membres.

    Les membres qui ne sont plus dans la conversation (ou plus éligibles à l'analyse)
    sont ignorés ; un seau qui n'a plus aucun membre présent disparaît. C'est le
    `safe-recenter-clusters` de Pol.is, et c'est ici que la dérive du repère est
    absorbée.
    """
    centres: dict[int, np.ndarray] = {}
    survivants: dict[int, set[int]] = {}
    for seau, membres in appartenances.items():
        presents = [positions[p] for p in membres if p in positions]
        if not presents:
            continue
        centres[seau] = np.mean(presents, axis=0)
        survivants[seau] = {p for p in membres if p in positions}
    return centres, survivants


def fusionne(
    centres: dict[int, np.ndarray],
    appartenances: dict[int, set[int]],
    tolerance: float,
) -> tuple[dict[int, np.ndarray], dict[int, set[int]]]:
    """Fusionne les seaux dont les centres se sont rejoints.

    L'identifiant conservé est celui du seau le plus peuplé : c'est la continuité la
    plus défendable, et c'est le choix de `merge-clusters` chez Pol.is.
    """
    restants = sorted(centres, key=lambda s: (-len(appartenances[s]), s))
    gardes: list[int] = []
    for seau in restants:
        jumeau = next(
            (
                g
                for g in gardes
                if float(np.linalg.norm(centres[seau] - centres[g])) <= tolerance
            ),
            None,
        )
        if jumeau is None:
            gardes.append(seau)
            continue
        appartenances[jumeau] = appartenances[jumeau] | appartenances[seau]
        appartenances.pop(seau)
    fusionnes = {s: appartenances[s] for s in gardes}
    return {s: centres[s] for s in gardes}, fusionnes


def point_le_plus_distal(
    positions: dict[int, np.ndarray], centres: dict[int, np.ndarray]
) -> tuple[int | None, float]:
    """Participant le plus éloigné du centre le plus proche de lui.

    Transcription de `most-distal` : on cherche le point que la couche explique le plus
    mal, c'est-à-dire celui qui justifie le mieux la création d'un seau.
    """
    if not centres or not positions:
        return None, 0.0
    valeurs = np.array(list(centres.values()))
    pire_pid, pire_dist = None, -1.0
    for pid, xy in positions.items():
        distance = float(np.min(np.linalg.norm(valeurs - xy, axis=1)))
        if distance > pire_dist:
            pire_pid, pire_dist = pid, distance
    return pire_pid, max(pire_dist, 0.0)


def scinde(
    centres: dict[int, np.ndarray],
    appartenances: dict[int, set[int]],
    positions: dict[int, np.ndarray],
    cible: int,
) -> tuple[dict[int, np.ndarray], dict[int, set[int]]]:
    """Ajoute des seaux, un par un, en détachant le point le plus distal.

    Boucle fidèle à `clean-start-clusters` : à chaque tour on retire le point de son
    seau, on en fait un seau neuf d'identifiant `max + 1` — jamais un identifiant
    recyclé, pour qu'un seau éteint ne ressuscite pas — puis **on recentre** avant le
    tour suivant. On s'arrête si plus aucun point n'est à distance non nulle : il n'y a
    alors plus rien à séparer.
    """
    possibles = min(cible, len({tuple(v) for v in positions.values()}))
    centres = dict(centres)
    appartenances = {s: set(m) for s, m in appartenances.items()}

    while len(centres) < possibles:
        pid, distance = point_le_plus_distal(positions, centres)
        if pid is None or distance <= 0:
            break
        for membres in appartenances.values():
            membres.discard(pid)
        neuf = max(appartenances, default=-1) + 1
        appartenances[neuf] = {pid}
        centres, appartenances = recentre(appartenances, positions)
        if neuf not in centres:  # pathologique : on ne boucle pas indéfiniment
            break
    return centres, appartenances


def prepare_couche(
    appartenances_precedentes: dict[int, set[int]],
    positions: dict[int, np.ndarray],
    cible: int,
) -> tuple[list[int], np.ndarray]:
    """Recentrage, fusion, scission — et rien d'autre.

    Renvoie les identifiants de seaux **dans l'ordre** des centres, parce que c'est cet
    ordre qui porte l'identité : `PolisKMeans` fait démarrer son groupe *i* sur le
    centre *i*, donc l'étiquette *i* du calcul est le seau `ids[i]` du tour précédent.
    """
    centres, appartenances = recentre(appartenances_precedentes, positions)
    if centres:
        centres, appartenances = fusionne(
            centres, appartenances, TOLERANCE_FUSION * _rayon(np.array(list(positions.values())))
        )
        centres, appartenances = scinde(centres, appartenances, positions, cible)
    ids = sorted(centres)
    if not ids:
        return [], None
    return ids, np.array([centres[s] for s in ids])


def couche_de_base(
    positions: dict[int, np.ndarray],
    appartenances_precedentes: dict[int, set[int]] | None,
    cible: int,
    random_state: int | None = None,
) -> tuple[dict[int, set[int]], dict[int, np.ndarray]]:
    """Calcule la couche du tour courant, à froid ou réamorcée.

    Sans état précédent — premier calcul d'une conversation, ou cache vidé — on part de
    la stratégie déterministe `polis` de red-dwarf (les premières lignes distinctes),
    exactement comme `init-clusters` chez Pol.is.
    """
    pids = sorted(positions)
    X = np.array([positions[p] for p in pids])
    distincts = len({tuple(v) for v in X})

    ids, centres_depart = (
        prepare_couche(appartenances_precedentes, positions, cible)
        if appartenances_precedentes
        else ([], None)
    )

    # Le nombre de groupes demandé au k-means est celui des seaux VIVANTS, pas la
    # cible. Forcer `k = cible` sur une couche déjà réamorcée demandait des seaux que
    # le nuage ne soutient pas : ceux qui restaient vides étaient malgré tout dotés
    # d'un identifiant neuf à chaque tour, et l'identité des seaux — la seule chose
    # que la couche existe pour transporter — se renouvelait à données inchangées.
    # La cible n'intervient donc qu'en amont, dans `prepare_couche`, pour dire jusqu'où
    # scinder ; ici, on prend acte de ce qu'elle a produit.
    k = max(1, min(len(ids), distincts)) if ids else max(1, min(cible, distincts))
    if centres_depart is not None and len(centres_depart) > k:
        ids, centres_depart = ids[:k], centres_depart[:k]

    if k == 1:
        return {0: set(pids)}, {0: X.mean(axis=0)}

    modele = PolisKMeans(
        n_clusters=k,
        init="polis",
        init_centers=centres_depart,
        random_state=random_state,
    ).fit(X)

    # L'étiquette i vient du centre de départ i, donc du seau ids[i] : c'est là que
    # l'identité d'un seau se transmet. Les seaux nés d'un remplissage par `polis`
    # reçoivent un identifiant neuf, au-dessus de tout ce qui a déjà servi.
    prochain = max(ids, default=-1) + 1
    correspondance: dict[int, int] = {}
    for etiquette in range(k):
        if etiquette < len(ids):
            correspondance[etiquette] = ids[etiquette]
        else:
            correspondance[etiquette] = prochain
            prochain += 1

    appartenances: dict[int, set[int]] = {}
    for pid, etiquette in zip(pids, modele.labels_):
        appartenances.setdefault(correspondance[int(etiquette)], set()).add(pid)
    centres = {
        correspondance[i]: modele.cluster_centers_[i]
        for i in range(k)
        if correspondance[i] in appartenances
    }
    return appartenances, centres


def centres_pour_k(
    centres_courants: np.ndarray, nuage: np.ndarray, k: int
) -> np.ndarray | None:
    """Dérive `k` centres de départ depuis la couche, par fusion ou scission.

    C'est ce qui remplace la mémoire par k de Pol.is : au lieu de conserver un
    découpage distinct pour chaque valeur de k candidate, chaque candidat est dérivé de
    l'unique découpage vivant.

      - trop de centres -> on fusionne les deux plus proches, en boucle ;
      - pas assez -> on ajoute le point du nuage le plus éloigné de tout centre.
    """
    if centres_courants is None or len(centres_courants) == 0:
        return None
    centres = [np.asarray(c, dtype=float) for c in centres_courants]

    while len(centres) > k:
        meilleure, paire = None, None
        for i in range(len(centres)):
            for j in range(i + 1, len(centres)):
                d = float(np.linalg.norm(centres[i] - centres[j]))
                if meilleure is None or d < meilleure:
                    meilleure, paire = d, (i, j)
        i, j = paire
        centres[i] = (centres[i] + centres[j]) / 2
        centres.pop(j)

    while len(centres) < k:
        valeurs = np.array(centres)
        distances = np.array(
            [float(np.min(np.linalg.norm(valeurs - point, axis=1))) for point in nuage]
        )
        if distances.max() <= 0:
            break
        centres.append(np.asarray(nuage[int(distances.argmax())], dtype=float))

    return np.array(centres) if centres else None
