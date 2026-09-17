"""Appariement des groupes entre deux exécutions.

Testé comme une fonction pure : c'est la pièce dont dépend toute la crédibilité de
l'affichage, et elle se raisonne entièrement sur des ensembles de participants.
"""

from app.analysis.matching import group_name, match_groups


def test_the_first_run_founds_the_identities() -> None:
    mapping = match_groups({0: {1, 2}, 1: {3, 4}}, {}, next_free_label=0)

    assert mapping == {0: 0, 1: 1}


def test_identical_composition_keeps_the_identities() -> None:
    precedent = {0: {1, 2}, 1: {3, 4}}

    mapping = match_groups({0: {1, 2}, 1: {3, 4}}, precedent, next_free_label=2)

    assert mapping == {0: 0, 1: 1}


def test_swapped_kmeans_labels_do_not_swap_the_identities() -> None:
    """Le cas qui motive tout : k-means a renuméroté, les gens n'ont pas bougé."""
    precedent = {0: {1, 2, 3}, 1: {4, 5, 6}}

    mapping = match_groups({0: {4, 5, 6}, 1: {1, 2, 3}}, precedent, next_free_label=2)

    assert mapping == {0: 1, 1: 0}


def test_a_slightly_changed_group_keeps_its_identity() -> None:
    """Une personne qui change de camp ne doit pas renommer les deux groupes."""
    precedent = {0: {1, 2, 3, 4}, 1: {5, 6, 7, 8}}

    mapping = match_groups(
        {0: {1, 2, 3}, 1: {4, 5, 6, 7, 8}}, precedent, next_free_label=2
    )

    assert mapping == {0: 0, 1: 1}


def test_a_genuinely_new_group_gets_a_fresh_identity() -> None:
    precedent = {0: {1, 2, 3, 4}, 1: {5, 6, 7, 8}}

    mapping = match_groups(
        {0: {1, 2}, 1: {5, 6}, 2: {3, 4, 7, 8}}, precedent, next_free_label=2
    )

    assert mapping[0] == 0
    assert mapping[1] == 1
    assert mapping[2] == 2  # le troisième groupe est neuf


def test_a_disappeared_identity_is_not_recycled() -> None:
    """Réutiliser l'identité d'un groupe disparu ferait resurgir un « groupe B »
    sans aucun rapport avec l'ancien."""
    precedent = {0: {1, 2}, 1: {3, 4}, 2: {5, 6}}

    mapping = match_groups({0: {1, 2, 3, 4}, 1: {7, 8}}, precedent, next_free_label=3)

    assert mapping[0] in {0, 1}      # hérite d'un des deux groupes fusionnés
    assert mapping[1] == 3           # aucun participant commun -> identité neuve


def test_a_group_without_any_common_participant_is_not_a_continuity() -> None:
    precedent = {0: {1, 2, 3}}

    mapping = match_groups({0: {7, 8, 9}}, precedent, next_free_label=1)

    assert mapping == {0: 1}


def test_the_assignment_is_global_not_greedy() -> None:
    """Un choix glouton prendrait le meilleur partenaire du premier groupe venu et
    dégraderait l'appariement d'ensemble ; l'affectation optimise le total.

    Groupe 0 recouvre 3 avec l'ancien 0 et 2 avec l'ancien 1.
    Groupe 1 recouvre 3 avec l'ancien 0 et 0 avec l'ancien 1.
    Glouton (0 -> ancien 0) laisserait le groupe 1 sans partenaire commun.
    L'optimum global est 0 -> ancien 1, 1 -> ancien 0 : total 5 contre 3.
    """
    precedent = {0: {1, 2, 3}, 1: {4, 5}}

    mapping = match_groups({0: {1, 2, 3, 4, 5}, 1: {1, 2, 3}}, precedent, 2)

    assert mapping == {0: 1, 1: 0}


def test_group_names_are_letters_not_numbers() -> None:
    """Un numéro suggère un classement et se confond avec l'étiquette technique."""
    assert group_name(0) == "A"
    assert group_name(1) == "B"
    assert group_name(25) == "Z"
    assert group_name(26) == "Z1"
    assert group_name(None) is None
