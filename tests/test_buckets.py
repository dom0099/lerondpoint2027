"""Couche intermédiaire de seaux (chantier D, option C').

Les cas couverts ici sont ceux que le D0 a désignés comme fragiles : un membre qui
disparaît, un seau qui se vide, deux centres qui se rejoignent, un participant qui
arrive et qu'il faut détacher — et surtout la propriété qui justifie toute
l'architecture : le recentrage absorbe un changement de repère.
"""

import numpy as np

from app.analysis import buckets


def _positions(mapping):
    return {pid: np.array(xy, dtype=float) for pid, xy in mapping.items()}


def test_recentring_reads_the_current_positions() -> None:
    """Un centre est recalculé, jamais transporté."""
    centres, membres = buckets.recentre(
        {0: {1, 2}}, _positions({1: (0.0, 0.0), 2: (2.0, 4.0)})
    )
    assert np.allclose(centres[0], [1.0, 2.0])
    assert membres[0] == {1, 2}


def test_recentring_absorbs_a_reflection_of_the_frame() -> None:
    """La propriété qui rend Procruste inutile (mesurée au D1).

    Les mêmes personnes, un repère retourné : le centre suit, sans qu'on ait eu à
    savoir que le repère avait changé.
    """
    avant = _positions({1: (1.0, 2.0), 2: (3.0, 4.0)})
    apres = {pid: np.array([-xy[0], -xy[1]]) for pid, xy in avant.items()}

    centre_avant, _ = buckets.recentre({0: {1, 2}}, avant)
    centre_apres, _ = buckets.recentre({0: {1, 2}}, apres)

    assert np.allclose(centre_apres[0], -centre_avant[0])


def test_a_member_who_left_is_ignored() -> None:
    """Un participant absent de la projection ne doit pas peser sur le centre."""
    centres, membres = buckets.recentre(
        {0: {1, 2, 99}}, _positions({1: (0.0, 0.0), 2: (2.0, 0.0)})
    )
    assert np.allclose(centres[0], [1.0, 0.0])
    assert membres[0] == {1, 2}          # 99 a disparu de l'appartenance aussi


def test_an_emptied_bucket_disappears() -> None:
    """Un seau sans aucun membre présent ne peut plus être recentré : il s'efface."""
    centres, membres = buckets.recentre(
        {0: {1}, 1: {42}}, _positions({1: (0.0, 0.0)})
    )
    assert set(centres) == {0}
    assert set(membres) == {0}


def test_redundant_centres_are_merged_keeping_the_largest_identity() -> None:
    centres = {3: np.array([0.0, 0.0]), 7: np.array([0.0001, 0.0])}
    membres = {3: {1, 2, 5}, 7: {9}}
    centres, membres = buckets.fusionne(centres, membres, tolerance=0.01)

    assert set(centres) == {3}                  # l'identité du plus peuplé survit
    assert membres[3] == {1, 2, 5, 9}           # aucun membre perdu


def test_distinct_centres_are_not_merged() -> None:
    centres = {0: np.array([0.0, 0.0]), 1: np.array([5.0, 0.0])}
    membres = {0: {1}, 1: {2}}
    centres, _ = buckets.fusionne(centres, membres, tolerance=0.01)
    assert set(centres) == {0, 1}


def test_splitting_detaches_the_most_distal_point() -> None:
    """Un nouvel arrivant très loin de tout doit obtenir son propre seau."""
    positions = _positions({1: (0.0, 0.0), 2: (0.1, 0.0), 3: (10.0, 10.0)})
    centres, membres = buckets.recentre({0: {1, 2, 3}}, positions)
    centres, membres = buckets.scinde(centres, membres, positions, cible=2)

    assert len(centres) == 2
    seul = [s for s, m in membres.items() if m == {3}]
    assert seul, f"le point distal n'a pas été détaché : {membres}"


def test_splitting_never_recycles_an_identifier() -> None:
    """Un seau neuf prend `max + 1` : ressusciter un identifiant éteint ferait
    reparaître un groupe sans rapport avec l'ancien (leçon de C6)."""
    positions = _positions({1: (0.0, 0.0), 2: (9.0, 9.0)})
    centres, membres = buckets.recentre({4: {1, 2}}, positions)
    centres, membres = buckets.scinde(centres, membres, positions, cible=2)
    assert max(membres) == 5


def test_splitting_stops_when_nothing_is_left_to_separate() -> None:
    """Des points confondus : aucune scission ne peut les distinguer."""
    positions = _positions({1: (1.0, 1.0), 2: (1.0, 1.0), 3: (1.0, 1.0)})
    centres, membres = buckets.recentre({0: {1, 2, 3}}, positions)
    centres, membres = buckets.scinde(centres, membres, positions, cible=3)
    assert len(centres) == 1


def test_the_layer_size_is_a_pass_through_on_small_conversations() -> None:
    """En dessous du plancher, autant de seaux que de participants : la couche ne
    change rien, exactement comme `base-k` = 100 chez Pol.is sur 30 participants."""
    assert buckets.taille_de_couche(5) == 5
    assert buckets.taille_de_couche(buckets.BASE_K_MIN) == buckets.BASE_K_MIN
    # Au-delà, elle amortit : ~4 participants par seau, plafonnée.
    assert buckets.taille_de_couche(120) == 30
    assert buckets.taille_de_couche(10_000) == buckets.BASE_K_MAX
    # Et le raccord se fait sans à-coup : le plancher porte sur les SEAUX, pas sur les
    # participants. L'avoir lu à l'envers demandait 20 seaux pour 24 personnes.
    assert buckets.taille_de_couche(11) == buckets.BASE_K_MIN
    assert buckets.taille_de_couche(24) == buckets.BASE_K_MIN
    assert buckets.taille_de_couche(44) == 11


def test_bucket_identity_survives_a_frame_flip() -> None:
    """Bout à bout : une couche calculée à froid, puis réamorcée dans un repère
    retourné, garde ses identifiants ET sa composition."""
    avant = _positions(
        {1: (0.0, 0.0), 2: (0.2, 0.1), 3: (5.0, 5.0), 4: (5.2, 4.9), 5: (-4.0, 3.0)}
    )
    appartenances, _ = buckets.couche_de_base(avant, None, cible=3, random_state=42)

    apres = {pid: np.array([-xy[0], -xy[1]]) for pid, xy in avant.items()}
    reamorcees, _ = buckets.couche_de_base(apres, appartenances, cible=3, random_state=42)

    assert set(reamorcees) == set(appartenances)
    for seau, membres in appartenances.items():
        assert reamorcees[seau] == membres


def test_the_layer_does_not_churn_on_unchanged_data() -> None:
    """À données rigoureusement inchangées, la couche ne bouge pas — ni composition,
    ni identifiants.

    C'est LA propriété que la couche existe pour tenir, et aucun test ne la couvrait
    dans son régime de travail : tous tournaient à dix participants ou moins, donc en
    traversée neutre, là où la couche ne fait rien et ne peut donc rien casser.

    Le défaut qu'il a fallu un banc sur base migrée pour voir : la cible réclamait plus
    de seaux que le nuage n'en soutient, et les seaux mort-nés consommaient un
    identifiant neuf à chaque tour — 5 seaux sur 11 renouvelés, 14 participants sur 24
    changeant de seau, identifiants croissant sans borne, pour une partition pourtant
    identique.
    """
    grille = {
        pid: (float(pid % 6) + 0.03 * pid, float(pid // 6) - 0.02 * pid)
        for pid in range(24)
    }
    positions = _positions(grille)
    cible = buckets.taille_de_couche(len(positions))
    assert cible < len(positions), "le régime de traversée neutre ne teste rien ici"

    tours = [buckets.couche_de_base(positions, None, cible, random_state=42)[0]]
    for _ in range(3):
        tours.append(
            buckets.couche_de_base(positions, tours[-1], cible, random_state=42)[0]
        )

    for rang, couche in enumerate(tours[1:], start=1):
        assert couche == tours[0], (
            f"la couche a bougé au tour {rang} sans qu'aucune donnée ne change :\n"
            f"  avant : {sorted(tours[0])}\n  après : {sorted(couche)}"
        )


def test_the_target_never_shrinks_an_existing_layer() -> None:
    """La cible dit jusqu'où scinder, elle ne démolit pas ce qui existe.

    Une couche plus fine que la cible — parce que des participants sont partis — doit
    être conservée telle quelle : un seau ne meurt que vidé ou fusionné.
    """
    positions = _positions({pid: (float(pid), 0.0) for pid in range(12)})
    fine = {pid: {pid} for pid in range(12)}          # 12 seaux
    couche, _ = buckets.couche_de_base(positions, fine, cible=4, random_state=42)
    assert set(couche) == set(fine)
    assert couche == fine


def test_candidate_centres_are_derived_from_the_single_layer() -> None:
    """Pas d'historique par k : chaque candidat se dérive par fusion ou scission."""
    couche = np.array([[0.0, 0.0], [0.1, 0.0], [8.0, 0.0]])
    nuage = np.array([[0.0, 0.0], [0.1, 0.0], [8.0, 0.0], [20.0, 0.0]])

    fusionnes = buckets.centres_pour_k(couche, nuage, 2)
    assert len(fusionnes) == 2

    scindes = buckets.centres_pour_k(couche, nuage, 4)
    assert len(scindes) == 4
    # Le point ajouté est le plus éloigné de tout centre existant.
    assert any(np.allclose(c, [20.0, 0.0]) for c in scindes)
