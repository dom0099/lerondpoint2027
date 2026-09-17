"""La modération des noms générés — chantier E5.

Décision du 3 septembre : **aucun nom généré n'apparaît sans validation d'un modérateur.**
Ce fichier éprouve les deux faces de cette phrase — ce qui ne s'affiche pas tant que
personne n'a tranché, et ce qui s'affiche une fois qu'on l'a fait.

La correction du nom est permise ici alors qu'elle ne l'est nulle part ailleurs dans la
modération de ce projet. Ce n'est pas un relâchement : ailleurs le texte est la parole
d'un participant, et la retoucher serait lui mettre des mots dans la bouche ; ici c'est la
sortie d'une machine, il n'y a personne à trahir.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import GroupNaming, ModerationStatus, MotifNommage
from app.services import nommage
from tests.conftest import open_conversation

MAINTENANT = datetime(2026, 9, 6, 21, 0, 0, tzinfo=timezone.utc)


async def _nomme(session, conversation_id, statements, *, groupe=0, nom="Un nom",
                 quand=MAINTENANT, statut=ModerationStatus.pending):
    ligne = GroupNaming(
        conversation_id=conversation_id,
        stable_group_id=groupe,
        decided_at=quand,
        motif=MotifNommage.nouveau,
        declarations=[[statements[0], "agree"], [statements[1], "disagree"]],
        nom=nom,
        named_at=quand,
        statut=statut,
        nom_valide=nom if statut is ModerationStatus.approved else None,
    )
    session.add(ligne)
    await session.commit()
    return ligne


async def test_a_produced_name_shows_nothing_until_a_human_says_so(
    moderator_client, session_factory
) -> None:
    """La phrase du 3 septembre, prise au mot : produit ne veut pas dire affichable."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat à valider"
    )
    async with session_factory() as session:
        await _nomme(session, conversation_id, statements, nom="Les opposants")
        assert await nommage.noms_affichables(session, conversation_id) == {}
        assert len(await nommage.a_moderer(session)) == 1


async def test_approving_publishes_the_name_as_generated(
    moderator_client, session_factory, moderator
) -> None:
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat validé"
    )
    async with session_factory() as session:
        ligne = await _nomme(session, conversation_id, statements, nom="Les opposants")
        await nommage.modere(session, ligne, moderator, approuve=True)
        affichables = await nommage.noms_affichables(session, conversation_id)
        assert affichables[0]["nom"] == "Les opposants"
        assert affichables[0]["corrige"] is False


async def test_a_corrected_name_is_marked_as_such(
    moderator_client, session_factory, moderator
) -> None:
    """Corriger est permis, mais ne doit pas pouvoir se faire passer pour valider.

    La mention publique du E6 dira « généré par IA, validé » ou « généré par IA, corrigé
    et validé ». Sans cette distinction elle mentirait un peu — et sur un outil de
    dialogue citoyen, une mention de transparence approximative vaut moins que pas de
    mention du tout. Le nom généré reste enregistré à côté, intact.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat corrigé"
    )
    async with session_factory() as session:
        ligne = await _nomme(
            session, conversation_id, statements, nom="Jeunes conducteurs soutenus"
        )
        await nommage.modere(
            session, ligne, moderator, approuve=True,
            nom_corrige="Favorables au permis à 16 ans",
        )
        affichables = await nommage.noms_affichables(session, conversation_id)
        assert affichables[0]["nom"] == "Favorables au permis à 16 ans"
        assert affichables[0]["corrige"] is True

        await session.refresh(ligne)
        assert ligne.nom == "Jeunes conducteurs soutenus", (
            "ce que le modèle a écrit doit rester lisible : c'est la seule trace qui "
            "permette de mesurer sa qualité réelle"
        )


async def test_a_rejected_name_never_appears(
    moderator_client, session_factory, moderator
) -> None:
    """Et le groupe garde sa lettre : refuser, c'est se taire, pas afficher autre chose."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat refusé"
    )
    async with session_factory() as session:
        ligne = await _nomme(session, conversation_id, statements, nom="Conservateurs")
        await nommage.modere(session, ligne, moderator, approuve=False)
        assert await nommage.noms_affichables(session, conversation_id) == {}
        await session.refresh(ligne)
        assert ligne.statut is ModerationStatus.rejected
        assert ligne.nom_valide is None


async def test_a_pending_new_name_does_not_erase_the_approved_one(
    moderator_client, session_factory, moderator
) -> None:
    """**Le défaut le plus coûteux que ce lot évite.**

    Les déclarations d'un groupe changent, la règle du E1 produit un nouveau nom, qui
    attend un modérateur. Si l'affichage suivait le dernier nom PRODUIT, le groupe
    retomberait sur « groupe B » à chaque déclenchement, le temps qu'un humain passe —
    l'écran clignoterait au rythme de la modération. C'est la même exigence de stabilité
    que le C6 sur l'identité des groupes.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat qui évolue"
    )
    async with session_factory() as session:
        ancien = await _nomme(
            session, conversation_id, statements, nom="Les opposants",
            quand=MAINTENANT - timedelta(days=1),
        )
        await nommage.modere(session, ancien, moderator, approuve=True)
        await _nomme(
            session, conversation_id, statements, nom="Un nom tout neuf",
            quand=MAINTENANT,
        )
        affichables = await nommage.noms_affichables(session, conversation_id)
        assert affichables[0]["nom"] == "Les opposants", (
            "le nom validé reste tant que le nouveau n'est pas tranché"
        )


async def test_refusing_a_new_name_keeps_the_previous_one(
    moderator_client, session_factory, moderator
) -> None:
    """Refuser un nouveau nom, c'est refuser un CHANGEMENT, pas effacer l'ancien."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat au refus"
    )
    async with session_factory() as session:
        ancien = await _nomme(
            session, conversation_id, statements, nom="Les opposants",
            quand=MAINTENANT - timedelta(days=1),
        )
        await nommage.modere(session, ancien, moderator, approuve=True)
        nouveau = await _nomme(
            session, conversation_id, statements, nom="Conservateurs", quand=MAINTENANT
        )
        await nommage.modere(session, nouveau, moderator, approuve=False)
        affichables = await nommage.noms_affichables(session, conversation_id)
        assert affichables[0]["nom"] == "Les opposants"


async def test_only_the_latest_pending_decision_of_a_group_is_offered(
    moderator_client, session_factory
) -> None:
    """Un débat actif peut empiler deux noms avant qu'un modérateur ne passe.

    Les montrer tous ferait trancher sur un état déjà démenti par le calcul suivant. Les
    plus anciens restent en base — ils sont l'historique — mais ne sont ni proposés ni
    affichables.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat pressé"
    )
    async with session_factory() as session:
        await _nomme(session, conversation_id, statements, nom="Le périmé",
                     quand=MAINTENANT - timedelta(hours=2))
        await _nomme(session, conversation_id, statements, nom="Le courant",
                     quand=MAINTENANT)
        a_moderer = await nommage.a_moderer(session)
        assert [n.nom for n in a_moderer] == ["Le courant"]


async def test_the_moderation_screen_shows_what_the_model_was_given(
    moderator_client, session_factory
) -> None:
    """Un nom seul ne se juge pas : l'écran doit porter les déclarations.

    « Les opposants à la baisse de l'âge du permis B » peut être juste ou exactement à
    l'envers, et rien dans la phrase ne le dit. Le chantier E a mesuré trois fois qu'un
    résultat bien formé peut être entièrement faux ; un modérateur qui validerait sur le
    seul nom validerait à l'aveugle.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat à relire"
    )
    async with session_factory() as session:
        ligne = await _nomme(session, conversation_id, statements)
        lisibles = await nommage.declarations_lisibles(session, ligne)
    assert len(lisibles) == 2
    sens = {s for _, s in lisibles}
    assert sens == {"pour", "contre"}, "les deux sens doivent être distingués à l'écran"


async def test_the_queue_page_offers_the_name_and_its_evidence(
    moderator_client, session_factory
) -> None:
    """Bout en bout : la file de modération porte le nom, ses déclarations, et les deux
    boutons. Le champ est PRÉ-REMPLI — valider sans toucher doit être un clic, corriger
    une retouche ; l'inverse ferait du cas courant le geste le plus coûteux."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat en file"
    )
    async with session_factory() as session:
        await _nomme(session, conversation_id, statements, nom="Les opposants")

    page = (await moderator_client.get("/moderation/queue")).text
    assert "Noms de groupe proposés" in page
    assert 'value="Les opposants"' in page
    # Sans l'apostrophe : le gabarit échappe, et c'est bien ce qu'on attend de lui.
    assert "amorce n°1." in page, "les déclarations doivent être visibles"
    assert "approuve" in page and "rejette" in page, "les deux sens doivent être lisibles"
    assert "/moderation/noms/" in page


async def test_moderating_twice_is_refused(moderator_client, session_factory) -> None:
    """Rejouer un lien de validation sur un nom déjà tranché le rouvrirait en silence.

    Même garde qu'au chantier C sur les conversations.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat rejoué"
    )
    async with session_factory() as session:
        ligne = await _nomme(session, conversation_id, statements, nom="Les opposants")
        naming_id = ligne.id

    premier = await moderator_client.post(
        f"/moderation/noms/{naming_id}/approve",
        data={"nom": "Les opposants"},
        follow_redirects=False,
    )
    assert premier.status_code == 303
    second = await moderator_client.post(
        f"/moderation/noms/{naming_id}/reject", follow_redirects=False
    )
    assert second.status_code == 409
