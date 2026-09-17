"""Lisseur du nombre de groupes affiché (chantier D).

Le D0 a mesuré un k qui saute de 2 à 5 entre répliques du même jeu de données. Ces
tests fixent le comportement attendu face à ce bruit.
"""

from app.analysis.smoothing import EtatLisseur, lisse


def test_the_first_run_displays_its_candidate() -> None:
    """Rien à lisser au premier tour : on n'a pas d'historique à opposer."""
    etat = lisse(EtatLisseur(), candidat=3, disponibles={2, 3, 4}, tours=3)
    assert etat.k_affiche == 3


def test_a_new_k_waits_for_the_buffer() -> None:
    etat = EtatLisseur(dernier_k=3, compte=3, k_affiche=3)
    etat = lisse(etat, candidat=4, disponibles={2, 3, 4}, tours=3)
    assert etat.k_affiche == 3 and etat.compte == 1     # premier tour du nouveau k
    etat = lisse(etat, candidat=4, disponibles={2, 3, 4}, tours=3)
    assert etat.k_affiche == 3 and etat.compte == 2
    etat = lisse(etat, candidat=4, disponibles={2, 3, 4}, tours=3)
    assert etat.k_affiche == 4 and etat.compte == 3     # trois tours d'accord


def test_an_oscillating_k_never_changes_the_display() -> None:
    """Le cas mesuré au D0 : un k qui bat entre deux valeurs d'un tour à l'autre.

    Sans lisseur, les participants verraient un groupe apparaître et disparaître à
    chaque recalcul, sans qu'aucun avis n'ait changé.
    """
    etat = lisse(EtatLisseur(), candidat=3, disponibles={2, 3, 4, 5}, tours=3)
    for candidat in (4, 3, 4, 3, 4, 3):
        etat = lisse(etat, candidat=candidat, disponibles={2, 3, 4, 5}, tours=3)
        assert etat.k_affiche == 3


def test_a_persistent_change_eventually_wins() -> None:
    """Un vrai changement ne doit pas être étouffé indéfiniment."""
    etat = lisse(EtatLisseur(), candidat=2, disponibles={2, 3}, tours=3)
    for _ in range(3):
        etat = lisse(etat, candidat=3, disponibles={2, 3}, tours=3)
    assert etat.k_affiche == 3


def test_a_vanished_k_falls_back_to_the_candidate() -> None:
    """Soupape de Pol.is : afficher un k qui n'a pas été calculé n'a aucun sens."""
    etat = EtatLisseur(dernier_k=5, compte=9, k_affiche=5)
    etat = lisse(etat, candidat=2, disponibles={2, 3}, tours=3)
    assert etat.k_affiche == 2


def test_the_counter_resets_when_the_candidate_changes() -> None:
    etat = EtatLisseur(dernier_k=4, compte=2, k_affiche=3)
    etat = lisse(etat, candidat=5, disponibles={3, 4, 5}, tours=3)
    assert etat.compte == 1 and etat.k_affiche == 3
