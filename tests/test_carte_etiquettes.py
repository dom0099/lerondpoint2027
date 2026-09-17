"""Les noms de groupe posés sur la carte 2D — chantier E6b.

Demande du client : que le nom d'un groupe apparaisse **sur** le nuage de points, sans
qu'on cesse de distinguer les points situés dessous. Les deux moitiés de cette phrase
sont éprouvées ici — celle qui montre, et celle qui laisse voir.

Comme le reste de `carte_rendu`, tout ce qui décide est calculé en Python et testé ici ;
le gabarit n'écrit que des balises.
"""

from app.services.carte import Carte, Enveloppe
from app.services.carte_rendu import (
    CARACTERES_PAR_LIGNE,
    LIGNES_MAX,
    PALETTE,
    dessin,
)


def _carte(groupes: dict[int, tuple[str, list[tuple[float, float]]]]) -> Carte:
    """Une carte à la main : par identité stable, sa lettre et ses points."""
    positions = [
        {"x": x, "y": y, "groupe": lettre}
        for lettre, points in groupes.values()
        for x, y in points
    ]
    enveloppes = [
        Enveloppe(name=lettre, size=len(points), sommets=None, stable_id=stable)
        for stable, (lettre, points) in groupes.items()
    ]
    return Carte(run_id=1, computed_at=None, positions=positions, groupes=enveloppes)


UN_GROUPE = {0: ("A", [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)])}


def test_a_map_without_any_validated_name_is_the_map_from_before() -> None:
    """Le cas de loin le plus fréquent : aucun nom validé, rien ne change."""
    dess = dessin(_carte(UN_GROUPE))
    assert dess.etiquettes == []
    assert len(dess.points) == 4


def test_a_validated_name_is_placed_on_its_own_cloud() -> None:
    """L'étiquette tombe au centre des points du groupe, pas ailleurs.

    Le centre du NUAGE et non celui du contour : un groupe en croissant a un contour
    dont le centre géométrique tombe dans le vide, entre ses deux branches. La moyenne
    des points, elle, tombe toujours là où il y a du monde.
    """
    dess = dessin(_carte(UN_GROUPE), {0: "Pour la gratuité"})
    assert len(dess.etiquettes) == 1
    etiquette = dess.etiquettes[0]
    assert etiquette.lignes == ["Pour la gratuité"]
    # Le centre du carré de points, dans l'espace du canevas.
    milieux_x = [p.cx for p in dess.points]
    assert min(milieux_x) < etiquette.cx < max(milieux_x)
    assert etiquette.couleur == PALETTE[0], "la couleur rattache le nom à son nuage"


def test_only_named_groups_get_a_label() -> None:
    """Un groupe sans nom validé garde sa lettre et rien d'autre."""
    carte = _carte(
        {
            0: ("A", [(0.0, 0.0), (0.1, 0.1)]),
            1: ("B", [(5.0, 5.0), (5.1, 5.1)]),
        }
    )
    dess = dessin(carte, {1: "Contre le projet"})
    assert [e.lignes for e in dess.etiquettes] == [["Contre le projet"]]


def test_a_long_name_wraps_without_cutting_a_word() -> None:
    """« constitution- / nel » se lit plus mal qu'un cartouche un peu large."""
    dess = dessin(
        _carte(UN_GROUPE), {0: "Les opposants à la baisse de l'âge du permis B"}
    )
    lignes = dess.etiquettes[0].lignes
    assert 1 < len(lignes) <= LIGNES_MAX
    assert all(len(ligne) <= CARACTERES_PAR_LIGNE + 2 for ligne in lignes)
    # Aucun mot n'a été coupé : recoller les lignes redonne le nom.
    assert " ".join(lignes) == "Les opposants à la baisse de l'âge du permis B"


def test_two_close_groups_do_not_stack_their_labels() -> None:
    """Deux cartouches superposés sont illisibles tous les deux.

    On décale plutôt que de masquer : un nom qu'on ne montre pas est une information
    perdue, un nom déplacé de quelques dizaines d'unités reste juste.
    """
    carte = _carte(
        {
            0: ("A", [(0.0, 0.0), (0.1, 0.0)]),
            1: ("B", [(0.0, 0.05), (0.1, 0.05)]),  # presque au même endroit
        }
    )
    dess = dessin(carte, {0: "Pour", 1: "Contre"})
    a, b = dess.etiquettes
    se_chevauchent = (
        a.x < b.x + b.largeur
        and b.x < a.x + a.largeur
        and a.y < b.y + b.hauteur
        and b.y < a.y + a.hauteur
    )
    assert not se_chevauchent, "les deux étiquettes se recouvrent encore"


def test_the_label_never_replaces_the_letter_in_the_legend() -> None:
    """La lettre reste l'identité stable du C6 ; le nom s'y ajoute, ailleurs."""
    dess = dessin(_carte(UN_GROUPE), {0: "Pour la gratuité"})
    assert dess.legende == [("A", PALETTE[0])]


async def test_the_cartridge_lets_the_points_underneath_show_through(
    client, moderator_client, session_factory
) -> None:
    """**L'exigence du client, vérifiée sur le SVG réellement rendu.**

    « Au-dessus du nuage de points mais de sorte à ce que l'on puisse distinguer les
    points situés en dessous. » Deux dispositifs y répondent et il en faut deux : un
    cartouche translucide, pour qu'on voie qu'il y a du monde dessous ; et un cerne
    blanc autour du texte, sans lequel la transparence rendrait le nom illisible sur
    les groupes denses — c'est-à-dire sur les plus intéressants.
    """
    from datetime import datetime, timezone

    from app.analysis import pipeline
    from app.models import Conversation, GroupNaming, ModerationStatus, MotifNommage
    from tests.conftest import open_conversation
    from tests.test_analysis import _populate

    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8, title="Débat cartographié"
    )
    async with session_factory() as session:
        await _populate(session, conversation_id, statements, n_participants=8)
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        await pipeline.analyse(session, conversation)
        session.add(
            GroupNaming(
                conversation_id=conversation_id,
                stable_group_id=0,
                decided_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
                motif=MotifNommage.nouveau,
                declarations=[],
                nom="Pour le passage à 16 ans",
                named_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
                statut=ModerationStatus.approved,
                nom_valide="Pour le passage à 16 ans",
            )
        )
        await session.commit()

    page = (await client.get(f"/c/{slug}")).text
    # Le PREMIER <svg> de la page est le logo du site : on vise la carte par son
    # étiquette d'accessibilité, qui est la seule chose qui la désigne à coup sûr.
    debut = page.index("Carte des groupes d'opinion")
    svg = page[debut : page.index("</svg>", debut)]
    assert "Pour le passage" in svg, "le nom doit être posé sur la carte"
    assert 'fill-opacity="0.72"' in svg, "le cartouche doit laisser voir les points"
    assert 'paint-order="stroke"' in svg, "le texte doit rester lisible par-dessus"
    # Et l'étiquette vient APRÈS les points dans le document : en SVG, l'ordre du
    # document EST l'ordre d'empilement, il n'y a pas de z-index.
    assert svg.index("<circle") < svg.index("paint-order")
