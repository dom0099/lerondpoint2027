"""L'identicon du profil (chantier profil) : déterministe, jamais un tirage."""

import re

from app.services.avatar import VARIANTES, identicon_svg


def test_the_same_seed_always_renders_the_same_svg() -> None:
    """La seule propriété qui compte : la même personne doit toujours voir le
    même dessin, d'un chargement de page à l'autre."""
    seed = "c0ffee00-0000-0000-0000-000000000000"
    assert identicon_svg(seed) == identicon_svg(seed)


def test_two_different_seeds_usually_render_differently() -> None:
    """Pas une preuve d'absence de collision — un hachage en a toujours en
    théorie — mais un garde-fou contre un bug qui rendrait tout le monde
    identique."""
    rendus = {identicon_svg(f"seed-{i}") for i in range(20)}
    assert len(rendus) > 1


def test_the_grid_is_mirrored_horizontally() -> None:
    """L'algorithme des identicons GitHub/GitLab : colonnes 3 et 4 recopient les
    colonnes 1 et 0. Vérifié en relisant les rectangles plutôt qu'en confiant à
    l'œil une propriété qui doit rester vraie pour tout `seed`."""
    for seed in ("a", "b", "c", "d", "e"):
        svg = identicon_svg(seed)
        remplies = {
            (int(x), int(y))
            for x, y in re.findall(r'x="(\d)" y="(\d)" width="1"', svg)
        }
        for x, y in remplies:
            assert (4 - x, y) in remplies


def test_the_color_variant_stays_within_the_declared_range() -> None:
    """La couleur n'est jamais écrite ici (voir l'en-tête du module) : seul un
    numéro de variante l'est, et il doit rester dans ce que `styles.css` déclare."""
    for i in range(50):
        svg = identicon_svg(f"seed-{i}")
        motif = re.search(r"identicon--(\d)", svg)
        assert motif is not None
        assert 0 <= int(motif.group(1)) < VARIANTES


def test_the_size_is_configurable() -> None:
    svg = identicon_svg("taille", taille=96)
    assert 'width="96"' in svg
    assert 'height="96"' in svg
