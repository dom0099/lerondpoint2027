"""La méthode Schulze (chantier Modération, MOD-9).

Aucune base : le module est pur, et ces tests le sont aussi. C'est ce qui permet de le
livrer **avant** son consommateur — MOD-10b le trouvera fiable au lieu de le découvrir en
même temps qu'il s'en sert.

Le fichier protège trois choses, dans cet ordre d'importance :

  1. **l'absence de partage des voix** — la raison d'être du lot : des reformulations
     voisines ne doivent pas s'affaiblir mutuellement ;
  2. **la conformité à la méthode** — vérifiée sur l'exemple de référence de la
     littérature, celui dont le résultat est publié ;
  3. **le déterminisme** — à égalité, l'ordre rendu ne doit dépendre de rien d'autre que
     de l'ancienneté du dépôt.
"""

from pathlib import Path

import pytest

from app.services.schulze import (
    BulletinInvalide,
    chemins_les_plus_forts,
    classer,
    classer_en_detail,
    preferences,
)


# --- 1. Le partage des voix, qui est la raison d'être du lot ------------------------


def test_des_reformulations_voisines_ne_se_divisent_pas_les_voix() -> None:
    """**Le test qui justifie tout le lot.**

    Trois personnes reformulent la même idée, chacune à sa façon (A1, A2, A3) ; deux
    préfèrent une autre proposition (B). Au premier tour, les trois variantes font une
    voix chacune et **B gagne avec deux voix** — alors que trois électeurs sur cinq
    préfèrent n'importe laquelle des variantes à B.

    C'est exactement ce qu'une navette de reformulation produirait, puisqu'elle encourage
    les variantes. Schulze compare deux à deux et n'a pas ce défaut.
    """
    bulletins = [
        ["A1", "A2", "A3", "B"],
        ["A2", "A3", "A1", "B"],
        ["A3", "A1", "A2", "B"],
        ["B", "A1", "A2", "A3"],
        ["B", "A2", "A3", "A1"],
    ]

    ordre = classer(bulletins, ["A1", "A2", "A3", "B"])

    # Au premier tour, B l'emporterait avec 2 voix contre 1, 1 et 1.
    premiers = [bulletin[0] for bulletin in bulletins]
    assert premiers.count("B") == 2
    assert all(premiers.count(f"A{n}") == 1 for n in (1, 2, 3))

    # Avec Schulze, les trois variantes passent toutes devant B.
    assert ordre[-1] == "B"
    assert set(ordre[:3]) == {"A1", "A2", "A3"}


def test_deux_quasi_doublons_ne_s_affaiblissent_pas_l_un_l_autre() -> None:
    """Le même défaut avec deux variantes seulement, et une majorité plus courte."""
    bulletins = [
        ["A1", "A2", "B"],
        ["A2", "A1", "B"],
        ["A1", "A2", "B"],
        ["B", "A1", "A2"],
        ["B", "A2", "A1"],
    ]

    ordre = classer(bulletins, ["A1", "A2", "B"])

    assert ordre[-1] == "B"


# --- 2. La conformité à la méthode --------------------------------------------------


def test_l_exemple_de_reference_donne_le_classement_publie() -> None:
    """**L'ancre de correction.** 45 votants, 5 candidats — l'exemple canonique de la
    littérature sur la méthode Schulze, dont le résultat publié est E > A > C > B > D.

    Si ce test tombe, c'est l'implémentation qui a bougé, pas l'exemple.
    """
    scrutin = [
        (5, "ACBED"), (5, "ADECB"), (8, "BEDAC"), (3, "CABED"),
        (7, "CAEBD"), (2, "CBADE"), (7, "DCEBA"), (8, "EBADC"),
    ]
    bulletins = [list(ordre) for combien, ordre in scrutin for _ in range(combien)]
    assert len(bulletins) == 45

    resultat = classer_en_detail(bulletins, list("ABCDE"))

    assert list(resultat.ordre) == ["E", "A", "C", "B", "D"]
    assert resultat.gagnant == "E"
    # Aucune égalité : l'exemple est franc, et le classement ne doit rien à l'ancienneté.
    assert resultat.egalites_tranchees == ()


def test_un_cycle_de_condorcet_a_trois_est_tranche_sans_boucler() -> None:
    """La raison d'être de la méthode : A bat B, B bat C, C bat A, et il faut sortir un
    ordre quand même.

    Le cycle est ici **parfaitement symétrique** : les trois candidats sont à égalité
    stricte, et c'est l'ancienneté du dépôt qui départage. On vérifie les deux choses —
    que l'ordre est bien celui du dépôt, et que les trois égalités sont **déclarées**
    plutôt que dissimulées dans un rang.
    """
    bulletins = [["A", "B", "C"], ["B", "C", "A"], ["C", "A", "B"]]

    resultat = classer_en_detail(bulletins, ["A", "B", "C"])

    assert list(resultat.ordre) == ["A", "B", "C"]
    assert set(resultat.egalites_tranchees) == {("A", "B"), ("A", "C"), ("B", "C")}


def test_un_cycle_desequilibre_se_tranche_par_les_chemins_et_non_par_l_anciennete() -> None:
    """Un cycle où un maillon est plus faible : là, la méthode décide toute seule, et
    l'ancienneté ne doit servir à rien."""
    bulletins = (
        [["A", "B", "C"]] * 5
        + [["B", "C", "A"]] * 4
        + [["C", "A", "B"]] * 3
    )

    resultat = classer_en_detail(bulletins, ["C", "B", "A"])  # dépôt dans l'autre sens

    assert resultat.gagnant == "A"
    assert resultat.egalites_tranchees == ()


def test_un_gagnant_de_condorcet_gagne() -> None:
    """Quand un candidat bat tous les autres en face à face, aucune méthode sérieuse ne
    doit le faire perdre."""
    bulletins = [
        ["B", "A", "C"],
        ["A", "B", "C"],
        ["A", "C", "B"],
    ]

    assert classer(bulletins, ["A", "B", "C"])[0] == "A"


# --- 3. Les cas limites -------------------------------------------------------------


def test_un_seul_candidat() -> None:
    assert classer([["A"], ["A"]], ["A"]) == ["A"]


def test_aucun_candidat() -> None:
    assert classer([], []) == []
    assert classer([]) == []


def test_aucun_bulletin_rend_les_candidats_dans_l_ordre_du_depot() -> None:
    """Personne n'a classé : il n'y a rien à agréger, mais il faut rendre un ordre. Celui
    du dépôt est le seul qui ne prétende rien."""
    assert classer([], ["C", "A", "B"]) == ["C", "A", "B"]


def test_une_egalite_stricte_est_tranchee_par_l_anciennete() -> None:
    """Un contre un, parfaitement symétrique. Le premier déposé passe devant — et
    l'égalité est déclarée."""
    resultat = classer_en_detail([["X", "Y"], ["Y", "X"]], ["X", "Y"])

    assert list(resultat.ordre) == ["X", "Y"]
    assert resultat.egalites_tranchees == (("X", "Y"),)

    # Et dans l'autre sens de dépôt, l'autre passe devant : c'est bien l'ancienneté qui
    # décide, pas l'ordre des bulletins.
    inverse = classer_en_detail([["X", "Y"], ["Y", "X"]], ["Y", "X"])
    assert list(inverse.ordre) == ["Y", "X"]


def test_un_bulletin_incomplet_ne_fait_pas_perdre_les_non_classes_entre_eux() -> None:
    """Un relecteur qui classe trois reformulations sur cinq n'a pas dit que les deux
    autres se valaient : il a dit qu'il ne les met pas au-dessus des trois. Les compter à
    égalité entre elles ne lui fait rien dire de plus."""
    compte = preferences([["A", "B"]], ("A", "B", "C", "D"))

    assert compte[("A", "B")] == 1
    assert compte[("A", "C")] == 1  # classé passe devant non classé
    assert compte[("B", "C")] == 1
    assert compte[("C", "D")] == 0  # deux non classés : aucune préférence
    assert compte[("D", "C")] == 0


def test_des_bulletins_incomplets_donnent_quand_meme_un_classement() -> None:
    bulletins = [["A", "B"], ["B", "C"], ["A"], ["C", "A"]]

    ordre = classer(bulletins, ["A", "B", "C"])

    assert sorted(ordre) == ["A", "B", "C"]
    assert ordre[0] == "A"


def test_un_candidat_que_personne_ne_classe_figure_quand_meme_au_classement() -> None:
    """Une reformulation que les cinq relecteurs ont ignorée doit apparaître, dernière,
    plutôt que de disparaître comme si elle n'avait pas été déposée."""
    ordre = classer([["A"], ["A"]], ["A", "Z"])

    assert ordre == ["A", "Z"]


def test_sans_liste_de_candidats_l_univers_vient_des_bulletins() -> None:
    """Ordre de première apparition : déterministe, donc testable — mais ce n'est PAS
    l'ancienneté, et le module le dit."""
    assert classer([["B", "A"], ["B", "A"]]) == ["B", "A"]


# --- 4. Les bulletins mal formés ----------------------------------------------------


def test_un_candidat_classe_deux_fois_dans_le_meme_bulletin_est_refuse() -> None:
    with pytest.raises(BulletinInvalide):
        classer([["A", "B", "A"]], ["A", "B"])


def test_un_bulletin_qui_classe_un_inconnu_est_refuse() -> None:
    """Refus plutôt qu'ignorance silencieuse : un identifiant inconnu est le signe d'un
    appelant qui s'est trompé de scrutin, pas d'une voix à écarter."""
    with pytest.raises(BulletinInvalide):
        classer([["A", "INCONNU"]], ["A", "B"])


def test_une_liste_de_candidats_avec_doublon_est_refusee() -> None:
    with pytest.raises(BulletinInvalide):
        classer([["A"]], ["A", "A"])


# --- 5. Le contrat du module --------------------------------------------------------


def test_le_module_est_pur_et_ne_modifie_pas_ses_entrees() -> None:
    """Il sera appelé par MOD-10b sur des données qui viennent de la base : les abîmer au
    passage serait invisible et coûteux."""
    bulletins = [["A", "B"], ["B", "A"]]
    candidats = ["A", "B"]
    copie_bulletins = [list(b) for b in bulletins]
    copie_candidats = list(candidats)

    classer(bulletins, candidats)

    assert bulletins == copie_bulletins
    assert candidats == copie_candidats


def test_le_resultat_est_deterministe() -> None:
    bulletins = [["A", "B", "C"], ["B", "C", "A"], ["C", "A", "B"], ["A", "C", "B"]]

    premier = classer(bulletins, ["A", "B", "C"])
    for _ in range(5):
        assert classer(bulletins, ["A", "B", "C"]) == premier


def test_les_forces_sont_rendues_pour_pouvoir_expliquer_un_rang() -> None:
    """Le jour où un auteur demandera pourquoi sa reformulation est deuxième, il faudra
    montrer autre chose qu'un rang."""
    resultat = classer_en_detail([["A", "B"], ["A", "B"], ["B", "A"]], ["A", "B"])

    assert resultat.forces[("A", "B")] > resultat.forces[("B", "A")]
    assert resultat.ordre[0] == "A"


def test_la_force_d_un_chemin_est_celle_de_son_maillon_le_plus_faible() -> None:
    """La propriété qui fait toute la méthode : A bat B par l'intermédiaire de C, et ce
    chemin ne vaut que ce que vaut sa victoire la plus serrée."""
    candidats = ("A", "B", "C")
    compte = {
        ("A", "C"): 8, ("C", "A"): 2,
        ("C", "B"): 6, ("B", "C"): 4,
        ("A", "B"): 0, ("B", "A"): 0,
    }

    force = chemins_les_plus_forts(compte, candidats)

    # Aucun lien direct A -> B, mais le chemin A -> C -> B vaut min(8, 6) = 6.
    assert force[("A", "B")] == 6


# --- 6. L'absence de consommateur, qui est une décision et non un oubli --------------


def test_le_module_n_a_encore_aucun_appelant() -> None:
    """**La garde du lot** — et le seul test de ce fichier qui soit fait pour mourir.

    Livrer un module avant son consommateur ne tient que si l'absence de consommateur
    est tenue. Sans cette garde, rien n'empêche un écran de brancher `classer()` « pour
    voir » : la navette de reformulation existerait alors dans le code avant d'avoir été
    menée une seule fois à la main (MOD-10a), et on aurait figé des hypothèses que
    personne n'a vérifiées.

    C'est la même façon de faire que `test_il_n_existe_aucune_case_d_acceptabilite` pour
    la grille : on protège une propriété *négative*, parce qu'elle se perd sans bruit.

    **Mode d'emploi** : le jour où MOD-10b branchera la navette, ce test tombera. Il se
    supprime **dans le commit qui branche**, jamais avant ni séparément — sa disparition
    est alors la trace, dans l'historique, que quelqu'un a décidé que l'heure était
    venue.
    """
    racine = Path(__file__).resolve().parents[1]
    module = racine / "app" / "services" / "schulze.py"
    extensions = {".py", ".html", ".js", ".mjs", ".sh"}

    appelants = []
    for dossier in ("app", "bin", "bancs", "migrations", "tests"):
        for chemin in sorted((racine / dossier).rglob("*")):
            if not chemin.is_file() or chemin.suffix not in extensions:
                continue
            if chemin == module or chemin == Path(__file__).resolve():
                continue
            if "schulze" in chemin.read_text(encoding="utf-8", errors="ignore").lower():
                appelants.append(str(chemin.relative_to(racine)))

    assert appelants == [], (
        "Le module Schulze a désormais un appelant : "
        + ", ".join(appelants)
        + ". Si c'est MOD-10b qui branche la navette, supprimez ce test dans le même "
        "commit. Sinon, c'est un branchement qui n'a pas été décidé."
    )
