"""Rendu de la carte : géométrie du dessin et tracé SVG (chantier D5).

Le tracé lui-même est un gabarit Jinja qui n'écrit que des balises ; tout ce qui décide
est dans `app/services/carte_rendu.py`. Les tests portent donc surtout là — et, pour
les quelques garanties qui ne se vérifient que sur le HTML produit (aucun identifiant,
pas de dimensions en pixels), sur le gabarit rendu pour de bon.
"""

import math
import random
import re

import pytest
from jinja2 import Environment, FileSystemLoader

from app.services import carte_rendu as rendu
from app.services.carte import Carte, Enveloppe, Visiteur


def _carte(groupes, positions, visiteur=None, run_id=7):
    return Carte(
        run_id=run_id,
        computed_at="2026-09-04T10:00:00+00:00",
        positions=positions,
        groupes=groupes,
        visiteur=visiteur,
    )


def _point(x, y, groupe):
    return {"x": x, "y": y, "groupe": groupe}


def _rendu(carte) -> str:
    """Rend le VRAI gabarit, pas une imitation."""
    env = Environment(loader=FileSystemLoader("app/templates"), autoescape=True)
    return env.get_template("public/_carte.html").render(**rendu.contexte(carte))


# --- la palette elle-même ----------------------------------------------------------


def test_the_official_palette_is_pinned() -> None:
    """Ces quatre valeurs viennent du client, pas du code.

    Tous les autres tests comparent à `PALETTE[i]` — ce qui est juste, puisqu'ils
    portent sur l'ATTRIBUTION des couleurs et non sur leur valeur. Mais cela veut dire
    qu'aucun d'eux ne verrait la palette changer. Celui-ci existe pour ça : l'identité
    visuelle n'est pas au code de la décider, et une modification doit se voir ici.

    Le D5 avait dû construire un jeu provisoire faute de trouver la palette dans le
    dépôt. Elle y a désormais un domicile, et une serrure.

    La serrure n'interdit pas de changer, elle oblige à le faire exprès : ces valeurs
    sont celles de la palette « colorblind safe » d'**IBM Design Language**, choisies
    par le client le 5 septembre 2026 — Okabe-Ito, adoptée la veille, couvrait tout le
    spectre et ne ressemblait à rien du site. Mettre ce test à jour fait partie du
    changement, ce n'est pas le contourner.
    """
    assert rendu.PALETTE == ("#648FFF", "#DC267F", "#634CCF", "#FE6100", "#FFB000")
    assert rendu.GRIS_RELIQUAT == "#9199A6"
    assert rendu.GRIS_RELIQUAT not in rendu.PALETTE
    assert len(set(rendu.PALETTE)) == len(rendu.PALETTE)


# --- attribution des couleurs ------------------------------------------------------


def test_the_largest_group_always_takes_the_first_colour() -> None:
    """La règle du G10, demandée par le client : la couleur suit le RANG D'EFFECTIF.

    Le plus grand groupe porte la première teinte, le deuxième la suivante, et ainsi de
    suite — quels que soient les noms des groupes et l'ordre dans lequel ils arrivent.
    Ce test passe les groupes dans le désordre exprès : c'est la taille qui classe, pas
    la position dans la liste.
    """
    groupes = [
        Enveloppe(name="A", size=3, stable_id=0),
        Enveloppe(name="B", size=9, stable_id=1),
        Enveloppe(name="C", size=5, stable_id=2),
    ]
    couleurs = rendu.couleurs_par_groupe(groupes)

    assert couleurs["B"] == rendu.PALETTE[0], "le plus grand groupe n'est pas bleu"
    assert couleurs["C"] == rendu.PALETTE[1]
    assert couleurs["A"] == rendu.PALETTE[2]


def test_the_d7_rank_swap_now_moves_the_colours_and_that_is_the_decision() -> None:
    """Ce que la règle du G10 coûte, éprouvé plutôt que seulement écrit.

    Le scénario est celui du banc du D7 : entre 50 et 150 participants, les groupes B
    et C échangent leur rang d'effectif — C passant de 24 à 52 membres, B de 16 à 65.
    Le D7 avait mesuré que **68 %** des participants voyaient alors la couleur de leur
    groupe changer alors que **86 %** n'avaient pas changé de groupe, et la règle
    d'identité existait pour l'empêcher.

    Le client a tranché en sens inverse le 5 septembre 2026. Ce test fige donc la
    conséquence : les deux teintes changent bien de main. Il n'est pas là pour dire que
    c'est bien — il est là pour que personne ne « répare » ce comportement en croyant
    corriger un bug, et pour que le jour où on voudra revenir en arrière, on sache
    exactement ce qui bouge.
    """
    a_50 = [
        Enveloppe(name="C", size=24, stable_id=2),
        Enveloppe(name="B", size=16, stable_id=1),
        Enveloppe(name="A", size=10, stable_id=0),
    ]
    a_150 = [
        Enveloppe(name="B", size=65, stable_id=1),
        Enveloppe(name="C", size=52, stable_id=2),
        Enveloppe(name="A", size=33, stable_id=0),
    ]

    avant = rendu.couleurs_par_groupe(a_50)
    apres = rendu.couleurs_par_groupe(a_150)

    # Le rang s'échange vraiment : le banc teste ce qu'il prétend tester.
    assert max(a_50, key=lambda g: g.size).name == "C"
    assert max(a_150, key=lambda g: g.size).name == "B"
    # Et la couleur suit le rang, donc elle change de main. C'est la décision.
    assert avant["C"] == rendu.PALETTE[0] and avant["B"] == rendu.PALETTE[1]
    assert apres["B"] == rendu.PALETTE[0] and apres["C"] == rendu.PALETTE[1]
    # A n'a pas bougé de rang : il ne bouge pas de couleur non plus.
    assert avant["A"] == apres["A"] == rendu.PALETTE[2]


def test_equal_sizes_are_separated_by_the_group_name() -> None:
    """Deux groupes de même effectif ne peuvent pas se disputer une teinte.

    Sans départage, l'ordre viendrait de celui des lignes rendues par la base — et la
    carte changerait de couleurs à données identiques, d'un rechargement à l'autre.
    """
    groupes = [
        Enveloppe(name="C", size=7, stable_id=2),
        Enveloppe(name="A", size=7, stable_id=0),
        Enveloppe(name="B", size=7, stable_id=1),
    ]
    couleurs = rendu.couleurs_par_groupe(groupes)

    assert couleurs["A"] == rendu.PALETTE[0]
    assert couleurs["B"] == rendu.PALETTE[1]
    assert couleurs["C"] == rendu.PALETTE[2]
    # Et l'ordre d'entrée n'y change rien.
    assert rendu.couleurs_par_groupe(list(reversed(groupes))) == couleurs


def test_no_two_displayed_groups_ever_share_a_colour() -> None:
    """L'invariant qui doit tenir quelle que soit la règle d'attribution.

    Un calcul produit au plus `K_MAX` groupes et la palette en compte autant : deux
    groupes affichés côte à côte ne peuvent jamais porter la même teinte. Éprouvé sur
    des tailles tirées au hasard, égalités comprises.
    """
    from app.analysis.staged import K_MAX

    rng = random.Random(7)
    for _ in range(200):
        n = rng.randint(2, K_MAX)
        groupes = [
            Enveloppe(name=chr(ord("A") + i), size=rng.randint(2, 40), stable_id=i)
            for i in range(n)
        ]
        couleurs = rendu.couleurs_par_groupe(groupes)
        teintes = [couleurs[g.name] for g in groupes]
        assert len(set(teintes)) == len(teintes), (
            f"deux groupes de même couleur : {[(g.name, g.size) for g in groupes]}"
        )


def test_a_group_of_one_gets_the_residue_grey() -> None:
    """Un groupe sous MIN_GROUP_SIZE n'est pas un groupe : jamais une couleur de la
    palette, et il ne consomme pas non plus une place dans l'ordre."""
    groupes = [
        Enveloppe(name="A", size=6, stable_id=0),
        Enveloppe(name="B", size=1, stable_id=1),
        Enveloppe(name="C", size=4, stable_id=2),
    ]
    couleurs = rendu.couleurs_par_groupe(groupes)

    assert couleurs["B"] == rendu.GRIS_RELIQUAT
    assert couleurs["B"] not in rendu.PALETTE
    # Le groupe d'une personne n'a décalé personne : A reste premier, C deuxième.
    assert couleurs["A"] == rendu.PALETTE[0]
    assert couleurs["C"] == rendu.PALETTE[1]


def test_no_group_colour_is_green() -> None:
    """Le vert appartient à « D'accord », et à rien d'autre.

    Les résultats par proposition montrent, sur la MÊME ligne, une pastille de groupe
    et des segments de réponse. Depuis que « D'accord » est vert (5 septembre 2026),
    une teinte verte dans la palette des groupes rendrait les deux confusables. La
    palette du client n'en contient aucune ; ce test empêche qu'une révision ultérieure
    en réintroduise une sans voir ce qu'elle casse ailleurs.

    « Vert » se lit ici comme : la composante verte domine franchement les deux autres.
    """
    for couleur in rendu.PALETTE:
        r, v, b = (int(couleur[i : i + 2], 16) for i in (1, 3, 5))
        assert not (v > r + 30 and v > b + 30), f"{couleur} est vert"


def test_the_legend_holds_only_real_groups() -> None:
    groupes = [
        Enveloppe(name="A", size=6),
        Enveloppe(name="B", size=1),
        Enveloppe(name="C", size=2),
    ]
    dessin = rendu.dessin(
        _carte(groupes, [_point(0, 0, "A"), _point(1, 1, "B"), _point(2, 0, "C")])
    )

    assert [nom for nom, _ in dessin.legende] == ["A", "C"]
    assert rendu.GRIS_RELIQUAT not in [couleur for _, couleur in dessin.legende]


def test_the_palette_covers_every_possible_group() -> None:
    """La lacune du D5 est close : cinq couleurs pour cinq groupes possibles.

    Le D5 avait dû figer le contraire — un cinquième groupe reprenant la couleur du
    premier — parce que la palette n'en comptait que quatre pour un `K_MAX` de 5. Ce
    test remplace celui-là et garde la porte : faire monter `K_MAX` sans allonger la
    palette redonnerait deux groupes de la même couleur.
    """
    from app.analysis.staged import K_MAX

    assert len(rendu.PALETTE) >= K_MAX, (
        f"{K_MAX} groupes possibles pour {len(rendu.PALETTE)} couleurs : "
        "deux groupes distincts porteraient la même teinte"
    )

    groupes = [
        Enveloppe(name=n, size=10 - i, stable_id=i) for i, n in enumerate("ABCDE")
    ]
    couleurs = rendu.couleurs_par_groupe(groupes)
    assert len(set(couleurs.values())) == 5


# --- géométrie ---------------------------------------------------------------------


def test_points_are_scaled_into_the_canvas_with_a_margin() -> None:
    dessin = rendu.dessin(
        _carte(
            [Enveloppe(name="A", size=4)],
            [_point(-3.0, -3.0, "A"), _point(5.0, 5.0, "A")],
        )
    )
    xs = [p.cx for p in dessin.points]
    ys = [p.cy for p in dessin.points]

    assert min(xs) == pytest.approx(rendu.MARGE)
    assert min(ys) == pytest.approx(rendu.MARGE)
    assert max(xs) == pytest.approx(rendu.CANEVAS - rendu.MARGE)
    assert max(ys) == pytest.approx(rendu.CANEVAS - rendu.MARGE)


def test_distances_are_not_stretched() -> None:
    """Une seule échelle pour les deux axes : étirer mentirait sur les écarts."""
    dessin = rendu.dessin(
        _carte(
            [Enveloppe(name="A", size=3)],
            [_point(0.0, 0.0, "A"), _point(10.0, 0.0, "A"), _point(0.0, 5.0, "A")],
        )
    )
    a, b, c = dessin.points
    largeur = b.cx - a.cx
    hauteur = c.cy - a.cy

    # 10 unités horizontales pour 5 verticales : le rapport doit être conservé.
    assert largeur / hauteur == pytest.approx(2.0)


def test_a_perfectly_flat_cloud_still_produces_a_canvas() -> None:
    """Tout le monde sur une ligne : hauteur non nulle, sans étirement des distances."""
    dessin = rendu.dessin(
        _carte(
            [Enveloppe(name="A", size=3)],
            [_point(x, 2.0, "A") for x in (0.0, 1.0, 2.0)],
        )
    )
    assert dessin.hauteur > 2 * rendu.MARGE
    assert dessin.largeur > dessin.hauteur


def test_everyone_at_the_same_spot_does_not_divide_by_zero() -> None:
    dessin = rendu.dessin(
        _carte([Enveloppe(name="A", size=3)], [_point(1.0, 1.0, "A")] * 3)
    )
    assert dessin is not None
    assert dessin.largeur > 0 and dessin.hauteur > 0


def test_nothing_to_draw_gives_nothing() -> None:
    assert rendu.dessin(None) is None
    assert rendu.dessin(_carte([], [])) is None


# --- le visiteur -------------------------------------------------------------------


def test_the_visitor_is_drawn_only_when_placed() -> None:
    positions = [_point(0.0, 0.0, "A"), _point(1.0, 1.0, "A"), _point(2.0, 0.0, "A")]
    groupes = [Enveloppe(name="A", size=3)]

    situe = rendu.dessin(
        _carte(groupes, positions, Visiteur(etat="situe", x=1.0, y=1.0, groupe="A"))
    )
    assert situe.visiteur is not None
    assert situe.visiteur.couleur == rendu.PALETTE[0]
    assert situe.message_visiteur is None


@pytest.mark.parametrize("etat", ["sous_le_seuil", "en_attente_de_calcul"])
def test_an_unplaced_visitor_gets_a_message_and_no_point(etat: str) -> None:
    """Jamais de point approché, jamais de point grisé « quelque part » : dans ces
    deux états, on ne SAIT pas où cette personne se situe."""
    dessin = rendu.dessin(
        _carte(
            [Enveloppe(name="A", size=3)],
            [_point(0.0, 0.0, "A"), _point(1.0, 1.0, "A"), _point(2.0, 0.0, "A")],
            Visiteur(etat=etat, raison="Il faut 5 votes pour être situé."),
        )
    )

    assert dessin.visiteur is None
    assert dessin.message_visiteur == "Il faut 5 votes pour être situé."


# --- lissage des contours ----------------------------------------------------------


def _lit_chemin(d: str):
    """Rend (sommets posés sur la courbe, poignées de chaque segment).

    Un analyseur minimal du `d` produit : les tests portent sur la géométrie, et la
    lire depuis la chaîne réellement écrite est le seul moyen de vérifier ce qui
    arrivera dans le SVG — pas ce que la fonction avait l'intention d'écrire.
    """
    jetons = d.replace(",", " ").split()
    sommets, poignees, courant, i = [], [], None, 0
    while i < len(jetons):
        if jetons[i] == "M":
            courant = (float(jetons[i + 1]), float(jetons[i + 2]))
            sommets.append(courant)
            i += 3
        elif jetons[i] == "C":
            b1 = (float(jetons[i + 1]), float(jetons[i + 2]))
            b2 = (float(jetons[i + 3]), float(jetons[i + 4]))
            fin = (float(jetons[i + 5]), float(jetons[i + 6]))
            poignees.append((courant, b1, b2, fin))
            courant = fin
            sommets.append(fin)
            i += 7
        elif jetons[i] == "L":
            courant = (float(jetons[i + 1]), float(jetons[i + 2]))
            sommets.append(courant)
            i += 3
        else:
            i += 1
    # Le dernier segment revient sur le point de départ : c'est le même sommet.
    if len(sommets) > 1 and math.dist(sommets[0], sommets[-1]) < 1e-9:
        sommets.pop()
    return sommets, poignees


def test_the_smoothed_outline_passes_through_every_vertex() -> None:
    """LA propriété qui justifie Catmull-Rom plutôt qu'un rabotage de coins.

    Un sommet d'enveloppe n'est pas un point de dessin : c'est la position d'un
    participant réel (`carte.enveloppe_concave` travaille sur le nuage lui-même). Une
    méthode qui coupe les angles — Chaikin, B-spline — pousserait ces personnes-là
    hors du contour de leur propre groupe. La courbe doit donc les traverser
    exactement, et seules les portions entre deux sommets ont le droit de bouger.
    """
    sommets = [(0.0, 0.0), (100.0, 10.0), (140.0, 90.0), (60.0, 150.0), (5.0, 80.0)]

    poses, _ = _lit_chemin(rendu.chemin_lisse(sommets))

    assert len(poses) == len(sommets)
    for attendu, obtenu in zip(sommets, poses):
        assert math.dist(attendu, obtenu) < 0.01


def test_the_outline_is_a_closed_curve() -> None:
    chemin = rendu.chemin_lisse([(0.0, 0.0), (100.0, 0.0), (50.0, 80.0)])

    assert chemin.startswith("M ")
    assert chemin.endswith("Z")
    # Autant de courbes que de côtés : le retour au départ en est un.
    assert chemin.count("C ") == 3


def test_no_handle_reaches_beyond_a_third_of_its_edge() -> None:
    """Le garde-fou, et il est géométrique.

    Une courbe de Bézier reste dans l'enveloppe convexe de ses points de contrôle.
    Plafonner les poignées à un tiers de l'arête borne donc l'écart entre la courbe et
    le segment droit qu'elle remplace : un contour ne peut pas partir chercher un
    point qui n'est pas à lui, quelle que soit la géométrie du groupe.

    Le cas éprouvé est celui qui met le plafond en défaut : une arête minuscule
    coincée entre deux très longues, ce que produit couramment une enveloppe concave.
    """
    sommets = [(0.0, 0.0), (400.0, 0.0), (402.0, 3.0), (200.0, 300.0)]

    _, poignees = _lit_chemin(rendu.chemin_lisse(sommets))

    # Tolérance : le `d` est écrit au centième d'unité de canevas. Sur un canevas de
    # 1000, deux coordonnées arrondies déplacent une poignée d'au plus 0,015 — c'est
    # cette écriture-là qu'on mesure, pas le plafond, qui est appliqué avant.
    arrondi = 0.02
    for depart, b1, b2, fin in poignees:
        plafond = math.dist(depart, fin) * rendu.POIGNEE_MAX + arrondi
        assert math.dist(depart, b1) <= plafond
        assert math.dist(fin, b2) <= plafond


def test_a_flat_smoothing_gives_back_the_straight_polygon(monkeypatch) -> None:
    """`LISSAGE = 0` doit redonner EXACTEMENT le polygone d'avant.

    C'est ce qui rend le réglage lisible : la constante ne fait qu'interpoler entre le
    tracé d'origine et la spline complète, elle ne déplace rien d'autre.
    """
    sommets = [(0.0, 0.0), (100.0, 10.0), (140.0, 90.0), (60.0, 150.0)]
    monkeypatch.setattr(rendu, "LISSAGE", 0.0)

    _, poignees = _lit_chemin(rendu.chemin_lisse(sommets))

    for depart, b1, b2, fin in poignees:
        assert math.dist(depart, b1) < 1e-9
        assert math.dist(fin, b2) < 1e-9


def test_a_degenerate_outline_falls_back_to_a_straight_path() -> None:
    """Sommets confondus : ni division par zéro, ni chemin vide.

    Trois points confondus, c'est un groupe dont les membres ont voté à l'identique —
    le cas que `carte.enveloppe_concave` documente déjà comme normal, et non de
    laboratoire.
    """
    doublons = [(10.0, 10.0), (10.0, 10.0), (90.0, 10.0), (90.0, 10.0), (50.0, 70.0)]
    chemin = rendu.chemin_lisse(doublons)
    poses, _ = _lit_chemin(chemin)

    assert chemin.endswith("Z")
    assert len(poses) == 3  # les doublons consécutifs sont écartés

    plat = rendu.chemin_lisse([(5.0, 5.0), (5.0, 5.0), (5.0, 5.0)])
    assert plat.startswith("M 5.00,5.00") and plat.endswith("Z")
    assert "C " not in plat  # un point unique ne se lisse pas, il se trace
    assert rendu.chemin_lisse([]) == ""


def test_vertices_that_differ_only_by_round_off_are_one_vertex() -> None:
    """Relevé sur la production, pas imaginé.

    L'enveloppe concave rend couramment des sommets séparés de 1e-15 : deux personnes
    ayant voté à l'identique, dont la projection ne diffère que par l'arrondi du
    calcul. Une égalité stricte les laisse passer, et ils coûtent deux fois — des
    segments de longueur nulle, et des tangentes calculées sur des distances de
    l'ordre de 1e-8 qui écrasent le lissage des arêtes voisines.
    """
    jumeau = (100.0, 10.0)
    sommets = [
        (0.0, 0.0),
        jumeau,
        (jumeau[0] + 2.7e-15, jumeau[1] - 4.4e-16),
        (jumeau[0] + 1.5e-15, jumeau[1]),
        (140.0, 90.0),
        (60.0, 150.0),
    ]

    chemin = rendu.chemin_lisse(sommets)
    poses, poignees = _lit_chemin(chemin)

    assert len(poses) == 4  # les trois jumeaux ne comptent que pour un
    # Aucun segment de longueur nulle : chacun mène quelque part.
    for depart, _, _, fin in poignees:
        assert math.dist(depart, fin) > rendu.EPSILON_SOMMET
    # Et les arêtes voisines gardent leur lissage : des poignées, pas des angles vifs.
    assert all(
        math.dist(depart, b1) > 0 or math.dist(fin, b2) > 0
        for depart, b1, b2, fin in poignees
    )


def test_smoothing_leaves_the_points_and_the_colours_alone() -> None:
    """Le lissage est un fait de tracé, et rien d'autre.

    Ni la position des participants ni la couleur des groupes ne bougent : le contour
    est la seule chose qui change entre les deux rendus.
    """
    carte = _carte(
        [
            Enveloppe(
                name="A", size=4, sommets=[[0, 0], [10, 1], [12, 9], [4, 12]]
            )
        ],
        [
            _point(0.0, 0.0, "A"),
            _point(10.0, 1.0, "A"),
            _point(12.0, 9.0, "A"),
            _point(4.0, 12.0, "A"),
        ],
    )

    lisse = rendu.dessin(carte)
    ancien_lissage = rendu.LISSAGE
    try:
        rendu.LISSAGE = 0.0
        droit = rendu.dessin(carte)
    finally:
        rendu.LISSAGE = ancien_lissage

    assert [(p.cx, p.cy, p.couleur) for p in lisse.points] == [
        (p.cx, p.cy, p.couleur) for p in droit.points
    ]
    assert lisse.legende == droit.legende
    assert (lisse.largeur, lisse.hauteur) == (droit.largeur, droit.hauteur)
    assert [c.couleur for c in lisse.contours] == [c.couleur for c in droit.contours]
    assert lisse.contours[0].chemin != droit.contours[0].chemin


def test_a_group_without_an_outline_draws_no_path() -> None:
    """Pas de contour, pas de chemin : un groupe de deux ne délimite pas une surface."""
    dessin = rendu.dessin(
        _carte(
            [Enveloppe(name="A", size=2)],
            [_point(0.0, 0.0, "A"), _point(1.0, 1.0, "A")],
        )
    )

    assert dessin.contours == []


# --- le SVG effectivement produit --------------------------------------------------


def test_the_svg_is_fluid_and_has_no_pixel_dimensions() -> None:
    html = _rendu(
        _carte(
            [Enveloppe(name="A", size=3, sommets=[[0, 0], [2, 0], [1, 2]])],
            [_point(0.0, 0.0, "A"), _point(2.0, 0.0, "A"), _point(1.0, 2.0, "A")],
        )
    )

    assert "viewBox=" in html
    assert 'width="100%"' in html
    assert not re.search(r'(width|height)="\d+px"', html)
    # Un `<path>` depuis le lissage des contours : ce sont les mêmes sommets,
    # reliés par une spline au lieu de segments droits.
    assert "<path" in html and "<circle" in html
    assert "<polygon" not in html


def test_the_outline_path_is_never_written_empty() -> None:
    """Le contrat entre le service et le gabarit, et il a déjà cédé une fois.

    Au déploiement du lissage, le gabarit demandait `contour.chemin` à un objet qui
    portait encore `contour.points` : Jinja rend l'attribut manquant par du vide, sans
    rien signaler. Le SVG restait valide, les contours avaient simplement disparu du
    site. Renommer le champ d'un côté seulement doit désormais faire tomber un test.
    """
    html = _rendu(
        _carte(
            [Enveloppe(name="A", size=4, sommets=[[0, 0], [2, 0], [2, 2], [0, 2]])],
            [_point(0.0, 0.0, "A"), _point(2.0, 0.0, "A"), _point(1.0, 2.0, "A")],
        )
    )

    assert 'd=""' not in html
    assert re.search(r'<path d="M [\d.]+,[\d.]+ C ', html)


def test_no_participant_identifier_reaches_the_html() -> None:
    """Aucun identifiant nulle part, y compris au survol : donc pas de `<title>`,
    pas d'attribut de données, rien à révéler."""
    html = _rendu(
        _carte(
            [Enveloppe(name="A", size=3, sommets=[[0, 0], [2, 0], [1, 2]])],
            [_point(0.0, 0.0, "A"), _point(2.0, 0.0, "A"), _point(1.0, 2.0, "A")],
            Visiteur(etat="situe", x=1.0, y=2.0, groupe="A"),
        )
    )

    assert "<title" not in html
    assert "data-participant" not in html
    assert "participant_id" not in html
    # Le `run_id` non plus : il sert à l'API, pas au dessin.
    assert "run_id" not in html


def test_the_residue_grey_never_appears_in_the_legend_markup() -> None:
    html = _rendu(
        _carte(
            [Enveloppe(name="A", size=4), Enveloppe(name="B", size=1)],
            [_point(0.0, 0.0, "A"), _point(1.0, 1.0, "A"), _point(9.0, 9.0, "B")],
        )
    )

    legende = html.split("<figcaption")[1]
    assert rendu.GRIS_RELIQUAT not in legende
    assert "Groupe A" in legende
    assert "Groupe B" not in legende
    # Mais le point de la personne isolée est bien dessiné, en gris.
    assert rendu.GRIS_RELIQUAT in html.split("<figcaption")[0]


# --- intégration dans la page de conversation --------------------------------------


async def test_the_map_appears_on_the_conversation_page(
    moderator_client, client, session_factory
) -> None:
    """La carte est tracée dans la page, dans le bloc « votre groupe »."""
    from app.analysis import pipeline
    from app.models import Conversation
    from tests.conftest import open_conversation
    from tests.test_carte import _peupler

    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements, prefixe="page")
        conversation = await session.get(Conversation, conversation_id)
        conversation.force_group_count = 2
        await session.commit()
        await pipeline.analyse(session, conversation)

    page = (await client.get(f"/c/{slug}")).text

    # `class="carte"` et non « il y a un <svg> » : depuis le chantier F3, l'anneau
    # de l'en-tête est lui aussi un SVG, présent sur toutes les pages. L'assertion
    # vise la carte, ce qu'elle a toujours voulu dire.
    assert 'class="carte"' in page and "viewBox=" in page
    assert "Groupe A" in page
    # La phrase d'accueil accompagne la carte, et prévient de ne pas lire les axes.
    # Espaces normalisés : le gabarit coupe ses lignes, le lecteur ne les voit pas.
    texte = re.sub(r"\s+", " ", page.replace("&nbsp;", " "))
    assert "deux points proches ont voté de façon semblable" in texte
    assert "seules comptent les distances" in texte
    # Elle est dans le même encadré que le texte du groupe, pas dans une section à part.
    #
    # Cet encadré est le panneau « La carte des avis » depuis le chantier I2
    # (instruction 11) ; c'était un `.card` avant. Le test est relu, pas supprimé : ce
    # qu'il garde — le dessin et la phrase qui le lit sont dans LE MÊME bloc — n'a pas
    # changé, seul le nom du bloc l'a fait.
    panneau = page.split('class="panneau panneau-carte"')[1].split("</div>{# /.colonne")[0]
    assert 'class="carte"' in panneau
    assert "groupe" in panneau, "la phrase qui situe la personne est dans le panneau"
    # La part de chaque groupe réel est écrite dans la légende, entre parenthèses —
    # les cartes de groupe qui la portaient sous la légende ont été retirées.
    legende = panneau.split('class="legende"')[1].split("</ul>")[0]
    assert len(re.findall(r"Groupe [A-Z] \(\d+%\)", legende)) == 2, "ce débat a deux groupes"
    assert 'class="carte-groupe"' not in panneau


async def test_no_map_before_the_first_computation(moderator_client, client) -> None:
    from tests.conftest import open_conversation

    _, slug, _ = await open_conversation(moderator_client)
    page = (await client.get(f"/c/{slug}")).text
    assert 'class="carte"' not in page


async def test_the_unplaced_visitor_is_told_once_not_twice(
    moderator_client, client, session_factory
) -> None:
    """Le bloc « votre groupe » porte déjà le message : la carte ne le répète pas.

    Deux fois la même phrase à trois centimètres d'intervalle ferait douter le lecteur
    d'avoir bien lu la première fois.
    """
    from app.analysis import pipeline
    from app.models import Conversation
    from tests.conftest import open_conversation
    from tests.test_carte import _peupler

    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8
    )
    async with session_factory() as session:
        await _peupler(session, statements, prefixe="muet")
        conversation = await session.get(Conversation, conversation_id)
        await pipeline.analyse(session, conversation)

    # Ce visiteur vote une seule fois : sous le seuil.
    detail = (await client.get(f"/api/conversations/{slug}")).json()
    await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": detail["statements"][0]["id"], "value": 1},
    )
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        await pipeline.analyse(session, conversation)

    page = (await client.get(f"/c/{slug}")).text

    assert page.count("Il faut 5 votes pour être situé") == 1
    # Et la carte est bien là, sans point pour lui.
    assert 'class="carte"' in page
