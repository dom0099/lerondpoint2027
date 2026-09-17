"""Le consommateur de la file de nommage — chantier E4.

Aucun modèle n'est appelé ici : `_appelle` est remplacé par une fonction d'essai. Ce qui
est éprouvé n'est pas la qualité des noms — c'est le banc du E2 qui la mesure, hors
production — mais **ce que le service fait de la réponse**, et surtout ce qu'il fait
quand elle est mauvaise, absente ou dangereuse.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import GroupNaming, MotifNommage
from app.services import nommage, nommage_llm
from tests.conftest import open_conversation

MAINTENANT = datetime(2026, 9, 6, 20, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def nommage_actif():
    """Le nommage est éteint par défaut : les tests l'allument, et le rendent éteint."""
    depart = settings.naming_enabled
    settings.naming_enabled = True
    yield
    settings.naming_enabled = depart


async def _demande(session, conversation_id, statements, *, groupe=0, quand=MAINTENANT):
    ligne = GroupNaming(
        conversation_id=conversation_id,
        stable_group_id=groupe,
        decided_at=quand,
        motif=MotifNommage.nouveau,
        declarations=[[statements[0], "pour"], [statements[1], "contre"]],
    )
    session.add(ligne)
    await session.commit()
    return ligne


def _repond(charge: dict):
    """Fabrique un faux modèle qui rend `charge`, et retient ce qu'on lui a envoyé."""
    vu = {}

    def faux(message, gbnf):
        vu["message"] = message
        vu["grammaire"] = gbnf
        return charge

    return faux, vu


async def test_a_name_is_produced_and_the_delay_recorded(
    moderator_client, session_factory, monkeypatch
) -> None:
    """Le cas nominal, et la mesure sur laquelle le E3 a écrit son seuil.

    `named_at - decided_at` est le délai entre l'empilement d'une demande et sa
    production. Sans lui, le seuil de réexamen de l'architecture (deux heures de médiane)
    serait un vœu plutôt qu'une mesure.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat à nommer"
    )
    faux, _ = _repond({0: {"nom": "Pour la réforme", "justification": "Parce que."}})
    monkeypatch.setattr(nommage_llm, "_appelle", faux)

    async with session_factory() as session:
        await _demande(session, conversation_id, statements)
        plus_tard = MAINTENANT + timedelta(minutes=20)
        assert await nommage_llm.produis(session, maintenant=plus_tard) == 1

        ligne = (await session.scalars(select(GroupNaming))).one()
        assert ligne.nom == "Pour la réforme"
        assert ligne.named_at == plus_tard
        assert (ligne.named_at - ligne.decided_at) == timedelta(minutes=20)
        assert ligne.erreur is None


async def test_the_model_never_receives_votes_or_identities(
    moderator_client, session_factory, monkeypatch
) -> None:
    """Ce qui sort de la machine : un titre et des déclarations. Rien d'autre.

    **C'est la garantie que le plan pose depuis le 3 septembre** — « lecture seule des
    déclarations représentatives déjà agrégées, jamais des votes bruts ni d'identité ».
    Elle est structurelle (`_invite` ne reçoit qu'un titre et des couples texte/sens,
    donc elle n'a aucun objet participant à portée), mais une garantie structurelle
    s'érode par ajouts successifs et bien intentionnés. Ce test la rend visible.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat discret"
    )
    faux, vu = _repond({0: {"nom": "Un nom", "justification": "Une phrase."}})
    monkeypatch.setattr(nommage_llm, "_appelle", faux)

    async with session_factory() as session:
        await _demande(session, conversation_id, statements)
        await nommage_llm.produis(session, maintenant=MAINTENANT)

    envoye = vu["message"]
    assert "Débat discret" in envoye
    for interdit in ("participant", "vote", "token", "jeton", "id=", "@"):
        assert interdit not in envoye.lower(), f"« {interdit} » ne doit pas être envoyé"
    # Le corps ne contient que des lignes de structure et des citations.
    for ligne in envoye.splitlines():
        assert ligne.strip() == "" or ligne.lstrip().startswith(
            ("Question du débat", "Groupe n°", "Ce groupe", "«")
        ), f"ligne inattendue dans l'invite : {ligne!r}"


async def test_two_groups_never_share_a_name(
    moderator_client, session_factory, monkeypatch
) -> None:
    """Le doublon vu au E2b : deux groupes distincts, le même nom.

    Ni la consigne ni la grammaire ne peuvent l'empêcher — l'une demande, l'autre
    contraint la forme et pas le sens. On vérifie donc après coup, et une collision fait
    échouer la demande : deux noms identiques sur une carte sont pires qu'un nom absent,
    parce qu'ils affirment quelque chose de faux au lieu de se taire.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat à doublon"
    )
    async with session_factory() as session:
        deja = await _demande(session, conversation_id, statements, groupe=0)
        deja.nom = "Les partisans"
        deja.named_at = MAINTENANT
        await session.commit()
        await _demande(session, conversation_id, statements, groupe=1)

        faux, _ = _repond({1: {"nom": "les partisans", "justification": "Idem."}})
        monkeypatch.setattr(nommage_llm, "_appelle", faux)
        assert await nommage_llm.produis(session, maintenant=MAINTENANT) == 0

        reste = (
            await session.scalars(
                select(GroupNaming).where(GroupNaming.stable_group_id == 1)
            )
        ).one()
        assert reste.nom is None
        assert "déjà porté" in reste.erreur


async def test_an_unavailable_model_leaves_everything_untouched(
    moderator_client, session_factory, monkeypatch
) -> None:
    """L'échec silencieux du plan : serveur éteint, site inchangé.

    Le nommage est un agrément ; le débat doit continuer sans lui. Une exception qui
    remonterait jusqu'au worker ferait tomber un passage d'analyse pour une raison qui
    n'a rien à voir avec le calcul des groupes.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat sans modèle"
    )

    def tombe(message, gbnf):
        raise ConnectionRefusedError("aucun serveur d'inférence")

    monkeypatch.setattr(nommage_llm, "_appelle", tombe)
    async with session_factory() as session:
        await _demande(session, conversation_id, statements)
        assert await nommage_llm.produis(session, maintenant=MAINTENANT) == 0
        ligne = (await session.scalars(select(GroupNaming))).one()
        assert ligne.nom is None and ligne.tentatives == 1
        assert "ConnectionRefusedError" in ligne.erreur


async def test_a_poisoned_request_is_abandoned_not_retried_for_ever(
    moderator_client, session_factory, monkeypatch
) -> None:
    """Trois échecs et on renonce, sinon une seule demande bloque toute la file.

    C'est ce qui distingue un échec silencieux d'un silence total : sans plafond, une
    déclaration qui fait suffoquer le modèle serait retentée à chaque passage et
    occuperait le consommateur pendant que les autres attendent.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat empoisonné"
    )

    def tombe(message, gbnf):
        raise ValueError("réponse illisible")

    monkeypatch.setattr(nommage_llm, "_appelle", tombe)
    async with session_factory() as session:
        await _demande(session, conversation_id, statements)
        for _ in range(settings.naming_max_attempts + 2):
            await nommage_llm.produis(session, maintenant=MAINTENANT)
        ligne = (await session.scalars(select(GroupNaming))).one()
        assert ligne.tentatives == settings.naming_max_attempts, (
            "au-delà du plafond, la demande ne doit plus être reprise"
        )


async def test_nothing_happens_while_naming_is_disabled(
    moderator_client, session_factory, monkeypatch
) -> None:
    """Le défaut : la file s'empile et personne ne la vide.

    C'est ce qui rend le E4 déployable sans rien changer au site. Allumer le nommage est
    un geste délibéré, fait le jour où un serveur d'inférence tourne pour de bon.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat en attente"
    )

    def jamais(message, gbnf):
        raise AssertionError("le modèle ne doit pas être appelé quand c'est désactivé")

    monkeypatch.setattr(nommage_llm, "_appelle", jamais)
    settings.naming_enabled = False
    async with session_factory() as session:
        await _demande(session, conversation_id, statements)
        assert await nommage_llm.produis(session, maintenant=MAINTENANT) == 0
        ligne = (await session.scalars(select(GroupNaming))).one()
        assert ligne.nom is None and ligne.tentatives == 0


async def test_the_oldest_request_is_served_first(
    moderator_client, session_factory, monkeypatch
) -> None:
    """Une file, pas une pile : sans cet ordre, un groupe malchanceux attend toujours."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat en file"
    )
    monkeypatch.setattr(settings, "naming_max_per_pass", 1)
    faux, vu = _repond({0: {"nom": "Le plus ancien", "justification": "."}})
    monkeypatch.setattr(nommage_llm, "_appelle", faux)

    async with session_factory() as session:
        await _demande(session, conversation_id, statements, groupe=1,
                       quand=MAINTENANT)
        await _demande(session, conversation_id, statements, groupe=0,
                       quand=MAINTENANT - timedelta(hours=3))
        await nommage_llm.produis(session, maintenant=MAINTENANT)
        nommes = list(
            await session.scalars(
                select(GroupNaming.stable_group_id).where(GroupNaming.nom.isnot(None))
            )
        )
        assert nommes == [0], "la demande la plus ancienne doit passer la première"


async def test_the_grammar_pins_the_exact_group_id(
    moderator_client, session_factory, monkeypatch
) -> None:
    """La grammaire fige l'identifiant demandé, et lui seul.

    Le E2b a mesuré qu'un modèle au format parfait pouvait ranger le contenu d'un groupe
    sous l'identifiant d'un autre — et que la grammaire, en imposant des identifiants
    impeccables, EFFACE le symptôme qui permettait de le voir. On n'envoie donc qu'un
    groupe à la fois : on retire au modèle l'occasion de se tromper, faute de pouvoir
    détecter qu'il s'est trompé.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat à un groupe"
    )
    faux, vu = _repond({7: {"nom": "Un nom", "justification": "."}})
    monkeypatch.setattr(nommage_llm, "_appelle", faux)
    async with session_factory() as session:
        await _demande(session, conversation_id, statements, groupe=7)
        await nommage_llm.produis(session, maintenant=MAINTENANT)
    assert '"\\"id\\"" ws ":" ws "7"' in vu["grammaire"]
    assert "groupe7" in vu["grammaire"]
    assert "Groupe n° 7" in vu["message"]


async def test_the_measurement_reports_the_delay_and_the_threshold(
    moderator_client, session_factory, monkeypatch
) -> None:
    """Le rapport porte le délai médian et dit si le seuil du E3 est franchi."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat mesuré"
    )
    faux, _ = _repond({0: {"nom": "Un nom", "justification": "."}})
    monkeypatch.setattr(nommage_llm, "_appelle", faux)
    async with session_factory() as session:
        await _demande(session, conversation_id, statements)
        # Trois heures d'attente : au-delà des deux heures du seuil.
        await nommage_llm.produis(session, maintenant=MAINTENANT + timedelta(hours=3))
        m = await nommage.mesure(session, depuis=MAINTENANT - timedelta(days=7))
        assert m["delai_median_s"] == 3 * 3600
        assert m["seuil_depasse"] is True
        assert m["en_attente"] == 0


async def test_the_other_groups_are_shown_but_only_one_is_named(
    moderator_client, session_factory, monkeypatch
) -> None:
    """Tous les groupes montrés, un seul nommé — et pourquoi ce n'est pas un détail.

    La première version du E4 n'envoyait qu'un groupe, pour supprimer toute occasion de
    confondre les identifiants. Mise en service, elle a nommé « Jeunes en faveur de la
    mobilité » le groupe qui s'OPPOSE au permis à 16 ans, là où le banc du E2b — qui
    montrait les deux groupes — rendait « Contre la réforme ».

    Ce que l'isolement retirait, c'est le contraste : la `repness` retient ce qui SÉPARE
    les groupes, donc leurs déclarations ne veulent dire quelque chose que rapportées à
    celles d'en face. La grammaire, elle, n'autorise toujours que l'identifiant visé.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat à deux camps"
    )
    faux, vu = _repond({0: {"nom": "Contre la réforme", "justification": "."}})
    monkeypatch.setattr(nommage_llm, "_appelle", faux)

    async with session_factory() as session:
        voisin = await _demande(session, conversation_id, statements, groupe=1)
        voisin.declarations = [[statements[1], "pour"], [statements[0], "contre"]]
        await session.commit()
        await _demande(session, conversation_id, statements, groupe=0)
        await nommage_llm.produis(session, maintenant=MAINTENANT)

    envoye = vu["message"]
    assert "Groupe n° 0" in envoye and "Groupe n° 1" in envoye, "les deux doivent être vus"
    assert "c'est CE groupe qu'il faut nommer" in envoye
    assert "Nomme uniquement le groupe n° 0." in envoye
    # La grammaire, elle, ne laisse toujours passer qu'un seul identifiant.
    assert "groupe1" not in vu["grammaire"]


async def test_two_names_that_read_the_same_are_a_duplicate(
    moderator_client, session_factory, monkeypatch
) -> None:
    """« Promoteurs de la marche traditionnelle » et « Partisans de la marche
    traditionnelle » sont le même nom.

    Collision réelle, observée à la mise en service, qu'une égalité de chaînes laissait
    passer. Rigoureusement distincts pour un `==`, indiscernables pour un lecteur — et
    c'est le lecteur qui compte.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat aux sosies"
    )
    async with session_factory() as session:
        deja = await _demande(session, conversation_id, statements, groupe=0)
        deja.nom = "Promoteurs de la marche traditionnelle"
        deja.named_at = MAINTENANT
        await session.commit()
        await _demande(session, conversation_id, statements, groupe=1)

        faux, _ = _repond(
            {1: {"nom": "Partisans de la marche traditionnelle", "justification": "."}}
        )
        monkeypatch.setattr(nommage_llm, "_appelle", faux)
        assert await nommage_llm.produis(session, maintenant=MAINTENANT) == 0

    assert nommage_llm._se_confondent(
        "Promoteurs de la marche traditionnelle",
        "Partisans de la marche traditionnelle",
    ), "deux synonymes sur le même sujet sont le même nom"

    # **Et surtout : deux positions opposées ne sont JAMAIS un doublon.** C'est le cas
    # le plus fréquent de tous — un débat à deux camps — et un filtre qui l'attraperait
    # laisserait la moitié des groupes sans nom. La position tranche avant le sujet.
    assert not nommage_llm._se_confondent(
        "Pour l'encadrement des loyers", "Contre l'encadrement des loyers"
    )
    assert not nommage_llm._se_confondent(
        "Favorables au permis à 16 ans", "Opposants au permis à 16 ans"
    )
    assert not nommage_llm._se_confondent(
        "Favorables à la piétonnisation du centre", "Opposants au projet de tramway"
    )


async def test_the_english_sense_from_red_dwarf_is_understood(
    moderator_client, session_factory, monkeypatch
) -> None:
    """Le E1 écrit « agree »/« disagree », l'invite parlait « pour »/« contre ».

    **Ce test vient d'un défaut passé en production.** Les deux vocabulaires se sont
    rencontrés à la mise en service et le décalage n'a levé aucune erreur : l'invite
    filtrait sur des mots absents, donc elle partait avec des en-têtes de groupe et
    aucune déclaration dessous. Le modèle nommait depuis le seul titre du débat et
    rendait des noms plausibles — « Jeunes pour le permis à 16 ans » pour le groupe qui
    s'y OPPOSE. Tous les indicateurs étaient au vert : JSON valide, nom produit, garde
    anti-doublon qui se déclenchait même.
    """
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat en anglais dans le texte"
    )
    faux, vu = _repond({0: {"nom": "Contre la réforme", "justification": "."}})
    monkeypatch.setattr(nommage_llm, "_appelle", faux)

    async with session_factory() as session:
        demande = await _demande(session, conversation_id, statements)
        # Le vocabulaire que red-dwarf produit réellement.
        demande.declarations = [
            [statements[0], "disagree"],
            [statements[1], "agree"],
        ]
        await session.commit()
        assert await nommage_llm.produis(session, maintenant=MAINTENANT) == 1

    envoye = vu["message"]
    assert "Ce groupe approuve :" in envoye and "Ce groupe rejette :" in envoye
    assert envoye.count("«") == 2, "les deux déclarations doivent être citées"


async def test_a_group_with_no_readable_statement_is_refused(
    moderator_client, session_factory, monkeypatch
) -> None:
    """Nommer à vide est pire que ne pas nommer : le nom serait entièrement inventé."""
    conversation_id, _, statements = await open_conversation(
        moderator_client, statements=3, title="Débat illisible"
    )

    def jamais(message, gbnf):
        raise AssertionError("on ne doit pas appeler le modèle sans rien à lui donner")

    monkeypatch.setattr(nommage_llm, "_appelle", jamais)
    async with session_factory() as session:
        demande = await _demande(session, conversation_id, statements)
        demande.declarations = [[statements[0], "vocabulaire-inconnu"]]
        await session.commit()
        assert await nommage_llm.produis(session, maintenant=MAINTENANT) == 0
        ligne = (await session.scalars(select(GroupNaming))).one()
        assert ligne.nom is None
        assert "vocabulaire-inconnu" in ligne.erreur
