"""L1 — la définition du clivage, éprouvée sans base et sans passage d'analyse.

**Aucun test de ce fichier ne touche la base ni red-dwarf.** Les nombres sont écrits à
la main : c'est ce qui permet d'éprouver la règle elle-même — la seule partie difficile
du chantier L — avant qu'une colonne ou un écran n'en dépende.

Deux familles de cas cohabitent ici, et il faut savoir laquelle on lit :

  - les cas **idéaux** (0 et 1 exacts), qui vérifient la forme de la définition ;
  - les cas **réalistes**, calculés avec le lissage de Laplace de red-dwarf
    (`p = (1 + n_v) / (2 + n)`), qui vérifient les seuils. Sur données réelles un
    clivage ne vaut jamais exactement 0 ni 1 ; un seuil réglé sur les cas idéaux ne
    voudrait donc rien dire.
"""

import math

from app.services.clivage import (
    CALCULS_POUR_ETIQUETTE,
    MIN_VUES_PAR_GROUPE,
    N_RETENUES,
    SEUIL_CLIVANTE,
    Score,
    ScoreDebat,
    Sens,
    agrege,
    merite_un_score,
    porte_etiquette,
    score_proposition,
    sens_de,
)


def p(n_pour: int, n_total: int) -> float:
    """La probabilité lissée de red-dwarf, pour écrire des cas réalistes."""
    return (1 + n_pour) / (2 + n_total)


def clivage_de(*groupes: tuple[int, int, int]) -> float:
    """Le clivage d'une proposition décrite groupe par groupe.

    Chaque groupe est donné en (nombre de pour, nombre de contre, nombre de votes),
    comme les colonnes `na` / `nd` / `ns` de red-dwarf.
    """
    accord = math.prod(p(pour, vus) for pour, _, vus in groupes)
    desaccord = math.prod(p(contre, vus) for _, contre, vus in groupes)
    score = score_proposition(accord, desaccord, [vus for _, _, vus in groupes])
    assert score is not None
    return score.clivage


# --- le score d'une proposition ----------------------------------------------------


def test_unanimous_groups_are_not_divisive() -> None:
    """Tous les groupes d'accord : consensus maximal, clivage nul."""
    score = score_proposition(1.0, 0.0, (10, 10))
    assert score == Score(clivage=0.0, consensus=1.0, sens=Sens.accord)


def test_unanimous_rejection_is_also_a_consensus() -> None:
    """Tous les groupes contre : c'est un accord, et le sens le dit."""
    score = score_proposition(0.0, 1.0, (10, 10))
    assert score == Score(clivage=0.0, consensus=1.0, sens=Sens.desaccord)


def test_a_perfect_split_between_two_groups_is_maximally_divisive() -> None:
    """Un groupe pour, l'autre contre : aucun des deux consensus ne se forme."""
    score = score_proposition(0.0, 0.0, (10, 10))
    assert score is not None
    assert score.clivage == 1.0
    assert score.consensus == 0.0


def test_the_two_faces_always_add_up_to_one() -> None:
    """Clivage et consensus sont la même mesure lue par les deux bouts."""
    score = score_proposition(0.36, 0.04, (6, 6))
    assert score is not None
    assert score.clivage + score.consensus == 1.0
    # Racine carrée de 0,36 : la moyenne géométrique, pas le produit brut.
    assert score.consensus == 0.6
    assert score.sens is Sens.accord


# --- la moyenne géométrique : le test qui attrape son absence ----------------------


def test_five_groups_and_two_groups_equally_divided_score_the_same() -> None:
    """Deux débats également clivés, l'un plus fragmenté : même score.

    Sans la racine k-ième, le débat à cinq groupes obtiendrait 0,998 contre 0,91 —
    et le tri du L4 classerait les débats par nombre de groupes plutôt que par
    désaccord.
    """
    par_groupe = 0.3
    deux = score_proposition(par_groupe**2, 0.001, (8, 8))
    cinq = score_proposition(par_groupe**5, 0.001, (8, 8, 8, 8, 8))
    assert deux is not None and cinq is not None
    assert math.isclose(deux.clivage, cinq.clivage)
    assert math.isclose(deux.clivage, 0.7)


# --- la règle du plancher ----------------------------------------------------------


def test_a_statement_one_group_barely_saw_gets_no_score_at_all() -> None:
    """Pas de score, et surtout pas un zéro : on ne sait pas, on se tait."""
    assert merite_un_score((10, MIN_VUES_PAR_GROUPE - 1)) is False
    assert score_proposition(0.0, 0.0, (10, MIN_VUES_PAR_GROUPE - 1)) is None


def test_the_floor_applies_to_each_group_not_to_the_total() -> None:
    """Trente votes dans un groupe et un dans l'autre ne disent rien du désaccord."""
    assert merite_un_score((30, 1)) is False
    assert merite_un_score((MIN_VUES_PAR_GROUPE, MIN_VUES_PAR_GROUPE)) is True


def test_a_single_group_debate_has_no_divisiveness_to_measure() -> None:
    """À un seul groupe, il n'y a rien « entre » quoi que ce soit."""
    assert merite_un_score((50,)) is False
    assert score_proposition(0.2, 0.7, (50,)) is None
    assert score_proposition(0.2, 0.7, ()) is None


def test_a_missing_consensus_never_becomes_a_zero() -> None:
    """Le `NaN` de l'alignement `gac_df` / `propositions_df` ne fait pas un clivage.

    Le prendre pour un zéro donnerait le clivage maximal à une proposition sur laquelle
    on ne sait rien — et la mettrait en tête du tri.
    """
    assert score_proposition(float("nan"), 0.5, (10, 10)) is None
    assert score_proposition(0.5, float("nan"), (10, 10)) is None
    assert score_proposition(None, 0.5, (10, 10)) is None
    assert score_proposition(1.5, 0.5, (10, 10)) is None


# --- les seuils, sur des valeurs telles que red-dwarf les produit ------------------


def test_a_divided_crowd_and_a_divided_debate_are_told_apart() -> None:
    """Le cas qui justifie le seuil, et la définition retenue au L0.

    Une foule indécise (chaque groupe se partage à 50/50) et deux camps constitués qui
    répondent l'inverse l'un de l'autre donnent le même 50/50 global. Le premier est du
    bruit, le second est exactement ce que le site existe pour montrer : seul le second
    passe le seuil.
    """
    unanime = clivage_de((4, 0, 4), (4, 0, 4))
    indecis = clivage_de((2, 2, 4), (2, 2, 4))
    fracture = clivage_de((4, 0, 4), (0, 4, 4))

    assert unanime < indecis < fracture
    assert unanime <= SEUIL_CLIVANTE
    assert indecis <= SEUIL_CLIVANTE
    assert fracture > SEUIL_CLIVANTE


def test_real_values_never_reach_the_ideal_bounds() -> None:
    """Le lissage de Laplace, constaté plutôt que supposé."""
    assert 0.0 < clivage_de((4, 0, 4), (4, 0, 4)) < clivage_de((4, 0, 4), (0, 4, 4)) < 1.0


# --- le sens du consensus, relu en base (L3) ---------------------------------------


def test_the_direction_is_read_from_the_raw_products() -> None:
    """« Les groupes l'approuvent » ou « les groupes la rejettent » : le L3 en a besoin.

    Comparer les produits bruts donne le même verdict que comparer les moyennes
    géométriques — la racine k-ième est croissante et l'exposant est le même pour les
    deux. Ce n'est donc pas un score recalculé à la lecture, c'est une direction.
    """
    assert sens_de(0.30, 0.02) is Sens.accord
    assert sens_de(0.02, 0.30) is Sens.desaccord
    # À égalité, l'accord l'emporte, comme dans `score_proposition`.
    assert sens_de(0.25, 0.25) is Sens.accord


def test_without_a_measure_there_is_no_direction() -> None:
    assert sens_de(None, 0.3) is None
    assert sens_de(float("nan"), 0.3) is None


# --- l'agrégation d'un débat -------------------------------------------------------


def debat(*clivages: float) -> list[Score]:
    """Un débat décrit par les seuls clivages de ses propositions."""
    return [
        Score(clivage=c, consensus=1.0 - c, sens=Sens.accord) for c in clivages
    ]


def test_a_debate_is_judged_on_its_sharpest_points_not_on_its_average() -> None:
    """Un point de fracture et vingt banalités doivent ressortir.

    La moyenne de tout donnerait 0,12 et rangerait ce débat derrière un débat tiède de
    bout en bout ; la moyenne des `N_RETENUES` plus clivantes dit ce qu'il contient.
    """
    tranchant = debat(0.9, 0.85, 0.8, 0.75, 0.7, *([0.05] * 20))
    score = agrege(tranchant)
    assert score is not None
    assert math.isclose(score.clivage, 0.8)


def test_a_debate_is_also_judged_on_the_agreements_it_reached() -> None:
    """Le versant consensus : la phrase d'accueil du site, mesurée.

    Même raisonnement retourné — la moyenne des `N_RETENUES` plus consensuelles, donc
    des moins clivantes, et non la moyenne de tout.
    """
    score = agrege(debat(0.9, 0.85, *([0.5] * 10), 0.1, 0.1, 0.1, 0.1, 0.1))
    assert score is not None
    assert math.isclose(score.consensus, 0.9)


def test_the_two_averages_are_independent() -> None:
    """Un débat peut être très clivant ET très consensuel : ce sont deux questions."""
    score = agrege(debat(*([0.95] * 5), *([0.02] * 5)))
    assert score is not None
    assert math.isclose(score.clivage, 0.95)
    assert math.isclose(score.consensus, 0.98)


def test_a_short_debate_still_gets_both_averages() -> None:
    """Moins de `N_RETENUES` propositions notées : la moyenne de ce qu'il y a.

    Les deux moyennes portent alors sur les mêmes propositions. C'est sans conséquence :
    chacune répond toujours à sa propre question.
    """
    propositions = debat(0.8, 0.4)
    assert len(propositions) < N_RETENUES
    score = agrege(propositions)
    assert score is not None
    assert math.isclose(score.clivage, 0.6)
    assert math.isclose(score.consensus, 0.4)
    assert (score.n_clivantes, score.n_notees) == (1, 2)


def test_a_debate_without_any_scored_statement_has_no_score() -> None:
    """« Pas encore mesuré » n'est pas « pas clivant » — le L4 en dépend pour ranger."""
    assert agrege([]) is None


def test_the_displayed_number_counts_statements_above_the_threshold() -> None:
    """Le seul nombre affichable : un entier, pas un score continu."""
    juste_dessus = SEUIL_CLIVANTE + 0.01
    juste_dessous = SEUIL_CLIVANTE - 0.01
    score = agrege(debat(juste_dessus, juste_dessus, SEUIL_CLIVANTE, juste_dessous))
    assert score is not None
    assert score.n_clivantes == 2
    assert score.n_notees == 4


# --- L4 : l'étiquette, et son hystérésis -------------------------------------------


def test_one_divisive_run_is_not_enough_for_the_label() -> None:
    """La règle du K0, transposée : il faut deux calculs, pas un.

    Le calcul tourne toutes les quinze minutes. Une étiquette posée sur le dernier
    calcul seul apparaîtrait et disparaîtrait entre deux visites sur un débat qui
    oscille autour du seuil, et une étiquette qui clignote est pire qu'une étiquette
    absente.
    """
    assert porte_etiquette([3]) is False
    assert porte_etiquette([3, 0]) is False
    assert porte_etiquette([3, 3]) is True


def test_the_label_goes_as_soon_as_one_run_stops_counting() -> None:
    """La pose demande deux calculs, le retrait un seul — et l'asymétrie est voulue.

    Elle penche du même côté qu'au K0 : le côté prudent est celui qui n'affirme rien.
    """
    assert porte_etiquette([0, 4, 4]) is False


def test_a_run_that_measured_nothing_never_carries_the_label() -> None:
    """`None` n'est pas zéro, mais l'étiquette se tait dans les deux cas.

    Un calcul à un seul groupe, ou dont aucune proposition ne franchit le plancher,
    n'a rien mesuré. On n'annonce pas ce qu'on n'a pas mesuré — c'est la même règle
    que « pas de score plutôt qu'un zéro » au L1, appliquée à un mot.
    """
    assert porte_etiquette([None, 4]) is False
    assert porte_etiquette([4, None]) is False
    assert porte_etiquette([None, None]) is False


def test_a_freshly_measured_debate_waits_its_second_run() -> None:
    """Un débat n'ayant qu'un seul calcul dans son histoire n'a pas d'étiquette."""
    assert porte_etiquette([]) is False
    assert len([2] * (CALCULS_POUR_ETIQUETTE - 1)) < CALCULS_POUR_ETIQUETTE
    assert porte_etiquette([2] * (CALCULS_POUR_ETIQUETTE - 1)) is False
    assert porte_etiquette([2] * CALCULS_POUR_ETIQUETTE) is True


def test_older_runs_never_revive_the_label() -> None:
    """Seuls les calculs RÉCENTS décident : un débat apaisé perd son étiquette.

    Le piège que ce test garde : une liste d'historique passée entière, dont on
    regarderait « au moins deux » au lieu de « les deux derniers ». Un débat clivant
    il y a six mois et calme depuis porterait alors l'étiquette pour toujours.
    """
    assert porte_etiquette([0, 0, 5, 5, 5]) is False
