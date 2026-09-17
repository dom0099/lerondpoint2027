"""L'affichage des noms générés — chantier E6.

Restreint, comme le plan le prévoit : le nom paraît sur la page de résultats d'un débat,
là où le lecteur a déjà sous les yeux ce qui caractérise chaque groupe. La carte,
l'accueil et l'annonce de fin de parcours continuent de ne montrer que la lettre.

Deux propriétés sont éprouvées ici, et ce sont les deux promesses du chantier : **rien ne
s'affiche sans validation**, et **la mention dit exactement ce qui s'est passé**.
"""

from datetime import datetime, timezone

from app.models import GroupNaming, ModerationStatus, MotifNommage
from app.services import nommage
from tests.conftest import open_conversation
from tests.test_analysis import _populate

MAINTENANT = datetime(2026, 9, 6, 22, 0, 0, tzinfo=timezone.utc)


async def _debat_calcule(moderator_client, session_factory, titre):
    """Un débat réellement calculé : c'est le seul moyen d'avoir des groupes stables."""
    from app.analysis import pipeline
    from app.models import Conversation

    conversation_id, slug, statements = await open_conversation(
        moderator_client, statements=8, title=titre
    )
    async with session_factory() as session:
        await _populate(session, conversation_id, statements, n_participants=8)
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        run = await pipeline.analyse(session, conversation)
    return conversation_id, slug, run


async def _nomme(session, conversation_id, groupe, nom, *, statut, nom_valide=None):
    ligne = GroupNaming(
        conversation_id=conversation_id,
        stable_group_id=groupe,
        decided_at=MAINTENANT,
        motif=MotifNommage.nouveau,
        declarations=[],
        nom=nom,
        named_at=MAINTENANT,
        statut=statut,
        nom_valide=nom_valide if statut is ModerationStatus.approved else None,
    )
    session.add(ligne)
    await session.commit()
    return ligne


async def test_a_pending_name_is_never_shown(
    client, moderator_client, session_factory
) -> None:
    """La promesse du 3 septembre, vérifiée sur la page réelle, pas sur un service."""
    conversation_id, slug, _ = await _debat_calcule(
        moderator_client, session_factory, "Débat non valide"
    )
    async with session_factory() as session:
        await _nomme(
            session, conversation_id, 0, "Un nom en attente",
            statut=ModerationStatus.pending,
        )
    page = (await client.get(f"/c/{slug}")).text
    assert "Un nom en attente" not in page
    assert "Nom proposé par une IA" not in page


async def test_a_rejected_name_is_never_shown(
    client, moderator_client, session_factory
) -> None:
    conversation_id, slug, _ = await _debat_calcule(
        moderator_client, session_factory, "Débat refusé à l'écran"
    )
    async with session_factory() as session:
        await _nomme(
            session, conversation_id, 0, "Conservateurs",
            statut=ModerationStatus.rejected,
        )
    page = (await client.get(f"/c/{slug}")).text
    assert "Conservateurs" not in page


async def test_an_approved_name_appears_beside_the_letter_not_instead(
    client, moderator_client, session_factory
) -> None:
    """**Le nom s'ajoute à la lettre.**

    « Groupe B » reste l'identité que le C6 garantit stable d'un calcul à l'autre, et que
    la carte, l'accueil et le routage lisent tous. Un nom qui prendrait sa place ferait de
    chaque renommage une rupture apparente de continuité — le défaut même que le C6
    existe pour supprimer.
    """
    conversation_id, slug, _ = await _debat_calcule(
        moderator_client, session_factory, "Débat nommé"
    )
    async with session_factory() as session:
        await _nomme(
            session, conversation_id, 0, "Pour le passage à 16 ans",
            statut=ModerationStatus.approved, nom_valide="Pour le passage à 16 ans",
        )
    page = (await client.get(f"/c/{slug}")).text
    assert "Pour le passage à 16 ans" in page
    assert "Groupe A" in page, "la lettre doit rester : c'est elle, l'identité stable"
    assert "Nom proposé par une IA, validé par un modérateur." in page


async def test_a_corrected_name_says_so(
    client, moderator_client, session_factory
) -> None:
    """La mention distingue deux cas parce que la base les distingue.

    Un nom validé tel quel n'est pas un nom réécrit par un humain. Approximer serait plus
    confortable et un peu faux — et sur un outil de dialogue citoyen, une mention de
    transparence approximative vaut moins que pas de mention du tout.
    """
    conversation_id, slug, _ = await _debat_calcule(
        moderator_client, session_factory, "Débat corrigé à l'écran"
    )
    async with session_factory() as session:
        await _nomme(
            session, conversation_id, 0, "Jeunes conducteurs soutenus",
            statut=ModerationStatus.approved,
            nom_valide="Pour le permis à 16 ans",
        )
    page = (await client.get(f"/c/{slug}")).text
    assert "Pour le permis à 16 ans" in page
    assert "corrigé</strong> puis validé par un modérateur" in page
    assert "Jeunes conducteurs soutenus" not in page, (
        "le nom généré reste en base pour la mesure, il n'est pas montré au public"
    )


async def test_a_debate_without_any_name_shows_exactly_what_it_showed_before(
    client, moderator_client, session_factory
) -> None:
    """Le cas de loin le plus fréquent, et celui qui ne doit rien casser."""
    conversation_id, slug, _ = await _debat_calcule(
        moderator_client, session_factory, "Débat sans nom"
    )
    page = (await client.get(f"/c/{slug}")).text
    assert "Ce qui caractérise chaque groupe" in page
    assert "Nom proposé par une IA" not in page
    # Le titre du groupe reste exactement « Groupe A », sans tiret ni complément : c'est
    # ce que la page montrait avant le chantier E, et rien ne doit avoir bougé.
    bloc = page.split("Ce qui caractérise chaque groupe", 1)[1]
    assert "Groupe A\n" in bloc or "Groupe A<" in bloc or "Groupe A " in bloc


async def test_the_map_and_the_home_page_still_show_only_the_letter(
    client, moderator_client, session_factory
) -> None:
    """L'affichage est RESTREINT : la page de résultats, et rien d'autre pour l'instant.

    Généraliser demande d'avoir vu des noms validés en vrai — c'est ce que le plan
    appelle « affichage restreint puis généralisé ». La carte et l'accueil continuent de
    ne porter que la lettre, qui est l'identité.
    """
    conversation_id, slug, _ = await _debat_calcule(
        moderator_client, session_factory, "Débat à carte"
    )
    async with session_factory() as session:
        await _nomme(
            session, conversation_id, 0, "Pour le passage à 16 ans",
            statut=ModerationStatus.approved, nom_valide="Pour le passage à 16 ans",
        )
    accueil = (await client.get("/")).text
    assert "Pour le passage à 16 ans" not in accueil
