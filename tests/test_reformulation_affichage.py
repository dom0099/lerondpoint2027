"""Ce que le lecteur voit d'une proposition reformulée (MOD-17).

**La règle que ce fichier protège, et elle a trois moitiés — c'est-à-dire qu'elle a été
tranchée en trois fois le même soir.** On dit au lecteur que le texte a été réécrit, et
sa date. On ne montre **pas la formulation précédente** : l'auteur ayant validé lui-même
la reformulation, exhiber l'ancienne reviendrait à montrer un brouillon qu'il a écarté
(§6.1 du plan v2). Et on ne dit **rien des votes** : ceux déjà portés restent valables, et
personne n'est invité à revoter.

**Deux des trois sont des absences**, et une absence ne se défend pas toute seule — c'est
la leçon de la garde d'absence d'appelant du MOD-9. La majorité des tests de ce fichier
vérifient donc ce qui ne doit PAS apparaître.

La seconde règle est celle de `signale_le` au chantier K, reprise telle quelle : **ce qui
est daté a eu lieu, ce qui n'est pas daté n'a pas eu lieu.** Une mention servie à tort
serait bien pire que le silence.
"""

from sqlalchemy import select

from app.models import Reformulation
from tests.conftest import open_conversation

TEXTE_CLAIR = "Il faut organiser des rencontres entre groupes qui ne se parlent pas."
TEXTE_ORIGINE = "Comment faire pour que les gens se parlent davantage entre eux ?"


async def _proposition_signalee(client, moderator_client, client_factory, titre="Débat MOD-17"):
    _, slug, _ = await open_conversation(moderator_client, statements=3, title=titre)
    depot = await client.post(
        f"/api/conversations/{slug}/statements", json={"text": TEXTE_ORIGINE}
    )
    assert depot.status_code == 201, depot.text
    statement_id = depot.json()["id"]
    async with client_factory() as signaleur:
        reponse = await signaleur.post(
            "/api/signalements",
            json={"statement_id": statement_id, "motifs": ["mal_formule"]},
        )
        assert reponse.status_code == 201
    return slug, statement_id


async def _navette(moderator_client, session_factory, statement_id, texte=TEXTE_CLAIR):
    reponse = await moderator_client.post(
        f"/moderation/signalements/{statement_id}/reformuler",
        data={"texte": texte},
        follow_redirects=False,
    )
    assert reponse.status_code == 303, reponse.text
    async with session_factory() as session:
        return await session.scalar(
            select(Reformulation).where(
                Reformulation.statement_id == statement_id,
                Reformulation.statut == "proposee",
            )
        )


def _proposition(detail, statement_id):
    return next(s for s in detail["statements"] if s["id"] == statement_id)


# --- ce qui n'a pas eu lieu ne se dit pas -------------------------------------------


async def test_une_proposition_jamais_reformulee_ne_porte_aucune_mention(
    client, moderator_client
) -> None:
    _, slug, ids = await open_conversation(
        moderator_client, statements=2, title="Débat sans navette"
    )
    detail = (await client.get(f"/api/conversations/{slug}")).json()

    for proposition in detail["statements"]:
        assert proposition["reformulee_le"] is None
        # Le texte d'avant n'est pas un champ servi, ni vide ni nul : il ne sort pas.
        assert "texte_origine" not in proposition


async def test_une_reformulation_en_attente_n_annonce_rien(
    client, client_factory, moderator_client, session_factory
) -> None:
    """**Le défaut le plus facile à commettre.** Une reformulation proposée n'a rien
    changé : l'annoncer dirait au lecteur qu'un texte a bougé alors qu'il est intact, et
    donnerait pour « texte d'origine » le texte qu'il a sous les yeux."""
    slug, statement_id = await _proposition_signalee(client, moderator_client, client_factory)
    await _navette(moderator_client, session_factory, statement_id)

    detail = (await client.get(f"/api/conversations/{slug}")).json()
    proposition = _proposition(detail, statement_id)

    assert proposition["text"] == TEXTE_ORIGINE
    assert proposition["reformulee_le"] is None


async def test_une_reformulation_refusee_n_annonce_rien(
    client, client_factory, moderator_client, session_factory
) -> None:
    slug, statement_id = await _proposition_signalee(client, moderator_client, client_factory)
    echange = await _navette(moderator_client, session_factory, statement_id)
    await client.post(f"/reformulations/{echange.id}/refuser", follow_redirects=False)

    proposition = _proposition(
        (await client.get(f"/api/conversations/{slug}")).json(), statement_id
    )

    assert proposition["reformulee_le"] is None


# --- ce qui a eu lieu se dit, et seulement ce qu'il faut ----------------------------


async def test_apres_acceptation_la_mention_est_servie_mais_jamais_le_texte_d_avant(
    client, client_factory, moderator_client, session_factory
) -> None:
    slug, statement_id = await _proposition_signalee(client, moderator_client, client_factory)
    echange = await _navette(moderator_client, session_factory, statement_id)
    await client.post(f"/reformulations/{echange.id}/accepter", follow_redirects=False)

    proposition = _proposition(
        (await client.get(f"/api/conversations/{slug}")).json(), statement_id
    )

    assert proposition["text"] == TEXTE_CLAIR
    assert proposition["reformulee_le"] is not None
    # **La décision de dom, et elle porte sur TOUTE la réponse, pas sur un champ.** Le
    # texte d'avant ne doit apparaître nulle part — ni dans un champ nommé, ni ailleurs
    # dans le corps servi.
    assert "texte_origine" not in proposition
    assert TEXTE_ORIGINE not in str(proposition)


async def test_la_date_est_en_toutes_lettres_comme_celle_d_un_lien_mort(
    client, client_factory, moderator_client, session_factory
) -> None:
    """La forme des dates publiques de ce site, tenue à un seul endroit
    (`chiffres.en_toutes_lettres`) : « 15 septembre 2026 », et non « 15/09 »."""
    from datetime import date

    from app.services.chiffres import en_toutes_lettres

    slug, statement_id = await _proposition_signalee(client, moderator_client, client_factory)
    echange = await _navette(moderator_client, session_factory, statement_id)
    await client.post(f"/reformulations/{echange.id}/accepter", follow_redirects=False)

    proposition = _proposition(
        (await client.get(f"/api/conversations/{slug}")).json(), statement_id
    )

    assert proposition["reformulee_le"] == en_toutes_lettres(date.today())


async def test_la_carte_de_vote_porte_la_mention(
    client, client_factory, moderator_client, session_factory
) -> None:
    """C'est la carte qui compte : c'est là que le texte est lu avant d'être voté."""
    slug, statement_id = await _proposition_signalee(client, moderator_client, client_factory)
    echange = await _navette(moderator_client, session_factory, statement_id)
    await client.post(f"/reformulations/{echange.id}/accepter", follow_redirects=False)

    vues = []
    for _ in range(6):
        suivante = (await client.get(f"/api/conversations/{slug}/next-statement")).json()
        if suivante.get("statement") is None:
            break
        vues.append(suivante["statement"])
        await client.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": suivante["statement"]["id"], "value": 0},
        )

    servie = next(s for s in vues if s["id"] == statement_id)
    assert servie["text"] == TEXTE_CLAIR
    assert servie["reformulee_le"] is not None
    assert TEXTE_ORIGINE not in str(servie)
    # Et les autres cartes de la même session ne portent rien.
    assert all(s["reformulee_le"] is None for s in vues if s["id"] != statement_id)


async def test_c_est_la_derniere_reformulation_que_le_service_retient(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Une proposition peut en connaître plusieurs à la suite.

    Éprouvé **au niveau du service** et non de la réponse servie : depuis que le texte
    d'avant ne sort plus, deux reformulations acceptées le même jour rendent la même
    date, et la page ne permet plus de les distinguer. La propriété reste vraie et reste
    utile — le registre du responsable s'en sert — donc elle reste gardée là où elle
    vit."""
    slug, statement_id = await _proposition_signalee(client, moderator_client, client_factory)
    premier = await _navette(moderator_client, session_factory, statement_id)
    await client.post(f"/reformulations/{premier.id}/accepter", follow_redirects=False)

    async with client_factory() as autre:
        await autre.post(
            "/api/signalements",
            json={"statement_id": statement_id, "motifs": ["mal_formule"]},
        )
    second = await _navette(
        moderator_client, session_factory, statement_id, texte="Une troisième version, plus nette."
    )
    await client.post(f"/reformulations/{second.id}/accepter", follow_redirects=False)

    from app.services.reformulation import acceptees_par_proposition

    proposition = _proposition(
        (await client.get(f"/api/conversations/{slug}")).json(), statement_id
    )
    assert proposition["text"] == "Une troisième version, plus nette."

    async with session_factory() as session:
        retenues = await acceptees_par_proposition(session, [statement_id])
    assert retenues[statement_id].texte_origine == TEXTE_CLAIR


# --- ce que la page porte, et ce qu'elle ne fait pas --------------------------------


async def test_la_page_ne_porte_aucun_endroit_ou_afficher_le_texte_d_avant(
    client, moderator_client
) -> None:
    """**Le test qui tient la décision dans le gabarit.** Une mention, et aucun dépli :
    il n'y a plus rien à déplier, et laisser l'emplacement inviterait à le remplir.

    La date est posée en `textContent`, comme tout ce qui vient d'une réponse JSON."""
    _, slug, _ = await open_conversation(
        moderator_client, statements=2, title="Débat pour le gabarit"
    )
    page = await client.get(f"/c/{slug}")

    assert 'id="reformulee-le"' in page.text
    assert "dateReformulation.textContent" in page.text
    # Ni le bloc dépliable de la reformulation, ni l'emplacement du texte d'avant, ni le
    # champ qui le transportait : les trois ont disparu ensemble, et aucun ne doit
    # revenir seul. L'assertion vise la reformulation NOMMÉMENT — la page porte d'autres
    # `details` (la seconde source, le panneau de proposition) qui n'ont rien à voir.
    assert 'class="reformulation"' not in page.text
    assert 'id="reformulation"' not in page.text
    assert 'id="texte-origine"' not in page.text
    assert "texte_origine" not in page.text


async def test_les_votes_ne_sont_pas_touches_par_l_affichage(
    client, client_factory, moderator_client, session_factory
) -> None:
    """**La moitié de la règle qu'on pourrait perdre sans s'en apercevoir.** Ce lot
    informe ; il ne remet aucun vote en cause et n'en redemande aucun."""
    from app.models import Vote

    slug, statement_id = await _proposition_signalee(client, moderator_client, client_factory)
    async with client_factory() as votant:
        await votant.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement_id, "value": 1},
        )
    async with session_factory() as session:
        avant = sorted(
            (v.participant_id, v.value)
            for v in await session.scalars(
                select(Vote).where(Vote.statement_id == statement_id)
            )
        )

    echange = await _navette(moderator_client, session_factory, statement_id)
    await client.post(f"/reformulations/{echange.id}/accepter", follow_redirects=False)
    await client.get(f"/api/conversations/{slug}")

    async with session_factory() as session:
        apres = sorted(
            (v.participant_id, v.value)
            for v in await session.scalars(
                select(Vote).where(Vote.statement_id == statement_id)
            )
        )
    assert apres == avant


async def test_aucune_invitation_a_revoter_nulle_part(
    client, client_factory, moderator_client, session_factory
) -> None:
    """dom a écarté l'avertissement des votants. La mention doit donc rester une mention :
    pas de « votre vote porte sur un texte modifié », pas de bouton, pas de rappel."""
    slug, statement_id = await _proposition_signalee(client, moderator_client, client_factory)
    async with client_factory() as votant:
        await votant.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement_id, "value": 1},
        )
        echange = await _navette(moderator_client, session_factory, statement_id)
        await client.post(f"/reformulations/{echange.id}/accepter", follow_redirects=False)

        accueil = await votant.get("/")
        debat = await votant.get(f"/c/{slug}")

    for page in (accueil, debat):
        assert "revoter" not in page.text.lower()
        assert "votre vote" not in page.text.lower()
