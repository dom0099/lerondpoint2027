"""La navette de reformulation (MOD-10).

**Ce que ce fichier protège avant tout : la décision de dom du 15 septembre 2026.** Les
votes portés sur une proposition reformulée restent valables, comme si rien n'avait
changé. C'est un arbitrage pris contre la recommandation du plan MOD-10a, et il n'est
tenable qu'à trois conditions, chacune tenue par un test ici :

  - l'auteur **consent** — le responsable ne peut jamais écrire dans le texte d'autrui ;
  - le texte d'origine est **conservé** ;
  - l'acte est **journalisé avec le texte d'origine**, dans une table en ajout seul.

Le test qui porte le lot est `test_les_votes_survivent_a_la_reformulation`.
"""

from sqlalchemy import select

from app.models import (
    ActeModeration,
    JournalModeration,
    ModerationStatus,
    Reformulation,
    Signalement,
    Statement,
    StatutReformulation,
    StatutSignalement,
    Vote,
)
from tests.conftest import open_conversation

TEXTE_CLAIR = "Il faut organiser des rencontres entre groupes qui ne se parlent pas."


async def _proposition_signalee(
    client,
    moderator_client,
    session_factory,
    client_factory,
    motifs=("mal_formule",),
    titre="Débat avec navette",
):
    """Un participant dépose, un tiers signale « mal formulé ». Rend (slug, id).

    `titre` est un paramètre parce qu'un test qui appelle ce helper plusieurs fois crée
    plusieurs débats : deux titres identiques font le même slug, et la création du second
    est refusée. Ce n'est pas un détail de test — c'est la garde d'unicité du chantier C
    qui fait son travail.
    """
    _, slug, _ = await open_conversation(
        moderator_client, statements=3, title=titre
    )
    depot = await client.post(
        f"/api/conversations/{slug}/statements",
        json={
            "text": f"Comment faire pour que les gens se parlent davantage ({titre}) ?"
        },
    )
    assert depot.status_code == 201, depot.text
    statement_id = depot.json()["id"]

    async with client_factory() as signaleur:
        reponse = await signaleur.post(
            "/api/signalements",
            json={"statement_id": statement_id, "motifs": list(motifs)},
        )
        assert reponse.status_code == 201, reponse.text
    return slug, statement_id


async def _proposer(moderator_client, statement_id, texte=TEXTE_CLAIR):
    return await moderator_client.post(
        f"/moderation/signalements/{statement_id}/reformuler",
        data={"texte": texte},
        follow_redirects=False,
    )


# --- le responsable propose ---------------------------------------------------------


async def test_proposer_ne_remplace_rien(
    client, client_factory, moderator_client, session_factory
) -> None:
    """**La différence entre une navette et une retouche éditoriale.** Le responsable
    propose ; le texte de l'auteur ne bouge pas tant que l'auteur n'a pas répondu."""
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    avant = None
    async with session_factory() as session:
        avant = (await session.get(Statement, statement_id)).text

    reponse = await _proposer(moderator_client, statement_id)

    assert reponse.status_code == 303
    async with session_factory() as session:
        statement = await session.get(Statement, statement_id)
        assert statement.text == avant
        echange = await session.scalar(
            select(Reformulation).where(Reformulation.statement_id == statement_id)
        )
        assert echange.statut is StatutReformulation.proposee
        assert echange.texte_propose == TEXTE_CLAIR
        assert echange.texte_origine == avant
        assert echange.repondu_le is None


async def test_proposer_journalise_l_acte(
    client, client_factory, moderator_client, session_factory
) -> None:
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    await _proposer(moderator_client, statement_id)

    async with session_factory() as session:
        actes = list(await session.scalars(select(JournalModeration)))
    proposees = [a for a in actes if a.acte is ActeModeration.reformulation_proposee]
    assert len(proposees) == 1
    assert proposees[0].cible_id == statement_id
    assert proposees[0].motif == TEXTE_CLAIR


async def test_sans_plainte_qui_la_demande_aucune_reformulation(
    client, client_factory, moderator_client, session_factory
) -> None:
    """**La garde qui empêche ce formulaire de devenir une porte d'édition.** Le
    responsable ne décide pas de ce qui est bien écrit : il répond à une plainte."""
    _, slug, _ = await open_conversation(moderator_client, statements=3, title="Sans plainte")
    depot = await client.post(
        f"/api/conversations/{slug}/statements",
        json={"text": "Une proposition que personne n'a signalée, et qui reste intacte."},
    )
    statement_id = depot.json()["id"]

    reponse = await _proposer(moderator_client, statement_id)

    assert reponse.status_code == 409
    async with session_factory() as session:
        assert (await session.get(Statement, statement_id)).text.startswith("Une proposition")


async def test_une_route_qui_ne_demande_pas_de_reformulation_ne_l_ouvre_pas(
    client, client_factory, moderator_client, session_factory
) -> None:
    """`fausse_information` part en `A_QUALIFIER` : la plainte existe, la navette non."""
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory,
        motifs=("fausse_information",),
    )
    assert (await _proposer(moderator_client, statement_id)).status_code == 409


async def test_une_seule_navette_a_la_fois(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Deux propositions concurrentes sur un même texte seraient une élection — donc le
    module d'agrégation de classements du MOD-9, donc des arbitres tirés au sort, qui
    n'existent pas encore.

    On ne le nomme pas : son test-sentinelle cherche le mot dans tout le dépôt, tests
    compris, et il a eu raison de m'arrêter en écrivant ce lot."""
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    assert (await _proposer(moderator_client, statement_id)).status_code == 303

    seconde = await _proposer(moderator_client, statement_id, texte="Une autre version encore.")

    assert seconde.status_code == 409
    async with session_factory() as session:
        echanges = list(
            await session.scalars(
                select(Reformulation).where(Reformulation.statement_id == statement_id)
            )
        )
    assert len(echanges) == 1


async def test_une_reformulation_vide_ou_identique_est_refusee(
    client, client_factory, moderator_client, session_factory
) -> None:
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    async with session_factory() as session:
        actuel = (await session.get(Statement, statement_id)).text

    assert (await _proposer(moderator_client, statement_id, texte="   ")).status_code == 409
    assert (await _proposer(moderator_client, statement_id, texte=actuel)).status_code == 409


async def test_un_texte_deja_porte_par_le_debat_est_refuse_au_responsable(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Le doublon est refusé au responsable et non à l'auteur : `uq_statement_text` le
    refuserait de toute façon, mais l'auteur serait devant un message qu'il ne peut pas
    corriger."""
    slug, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    voisine = await client.post(
        f"/api/conversations/{slug}/statements", json={"text": TEXTE_CLAIR}
    )
    assert voisine.status_code == 201

    assert (await _proposer(moderator_client, statement_id)).status_code == 409


# --- l'auteur accepte ---------------------------------------------------------------


async def test_accepter_remplace_le_texte(
    client, client_factory, moderator_client, session_factory
) -> None:
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    await _proposer(moderator_client, statement_id)
    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))

    reponse = await client.post(
        f"/reformulations/{echange.id}/accepter", follow_redirects=False
    )

    assert reponse.status_code == 303
    async with session_factory() as session:
        assert (await session.get(Statement, statement_id)).text == TEXTE_CLAIR
        apres = await session.get(Reformulation, echange.id)
        assert apres.statut is StatutReformulation.acceptee
        assert apres.repondu_le is not None
        # Ce sur quoi les votes conservés ont porté reste lisible.
        assert apres.texte_origine != TEXTE_CLAIR


async def test_les_votes_survivent_a_la_reformulation(
    client, client_factory, moderator_client, session_factory
) -> None:
    """**LE test du lot.** dom a tranché : les votes portés sur une proposition
    reformulée restent valables, comme si rien n'avait changé.

    Il compte les lignes ET leur valeur : un lot qui « conserverait » les votes en les
    remettant à zéro, ou en les recréant neutres, passerait un test qui se contenterait
    de compter."""
    slug, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    async with client_factory() as votant_1:
        await votant_1.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement_id, "value": 1},
        )
    async with client_factory() as votant_2:
        await votant_2.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement_id, "value": -1},
        )

    async with session_factory() as session:
        avant = sorted(
            (v.participant_id, v.value)
            for v in await session.scalars(
                select(Vote).where(Vote.statement_id == statement_id)
            )
        )
    assert len(avant) == 2

    await _proposer(moderator_client, statement_id)
    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))
    await client.post(f"/reformulations/{echange.id}/accepter", follow_redirects=False)

    async with session_factory() as session:
        apres = sorted(
            (v.participant_id, v.value)
            for v in await session.scalars(
                select(Vote).where(Vote.statement_id == statement_id)
            )
        )
        assert (await session.get(Statement, statement_id)).text == TEXTE_CLAIR
    assert apres == avant


async def test_accepter_clot_les_plaintes_qui_demandaient_la_reformulation(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Et **seulement celles-là** : une proposition signalée aussi pour fausse
    information reste à qualifier."""
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    async with client_factory() as autre:
        await autre.post(
            "/api/signalements",
            json={"statement_id": statement_id, "motifs": ["fausse_information"]},
        )

    await _proposer(moderator_client, statement_id)
    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))
    await client.post(f"/reformulations/{echange.id}/accepter", follow_redirects=False)

    async with session_factory() as session:
        signalements = list(
            await session.scalars(
                select(Signalement).where(Signalement.statement_id == statement_id)
            )
        )
    par_route = {s.route: s.statut for s in signalements}
    assert par_route["A_REFORMULER"] is StatutSignalement.traite
    assert par_route["A_QUALIFIER"] is StatutSignalement.recu


async def test_accepter_journalise_le_texte_d_origine(
    client, client_factory, moderator_client, session_factory
) -> None:
    """**Le seul acte du journal qui modifie un texte déjà voté**, et le seul à porter
    l'ancien. Sans cette ligne, « les votes restent valables » ne serait plus vérifiable
    par personne."""
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    async with session_factory() as session:
        origine = (await session.get(Statement, statement_id)).text

    await _proposer(moderator_client, statement_id)
    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))
    await client.post(f"/reformulations/{echange.id}/accepter", follow_redirects=False)

    async with session_factory() as session:
        actes = list(await session.scalars(select(JournalModeration)))
    acceptees = [a for a in actes if a.acte is ActeModeration.reformulation_acceptee]
    assert len(acceptees) == 1
    assert origine in acceptees[0].motif


# --- l'auteur refuse ----------------------------------------------------------------


async def test_refuser_ne_change_rien_et_laisse_la_plainte_ouverte(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Un refus rend la main au responsable au lieu de la lui prendre. Clore le dossier
    sur un refus ferait du silence de l'auteur une décision de modération."""
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    async with session_factory() as session:
        origine = (await session.get(Statement, statement_id)).text
    await _proposer(moderator_client, statement_id)
    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))

    reponse = await client.post(
        f"/reformulations/{echange.id}/refuser", follow_redirects=False
    )

    assert reponse.status_code == 303
    async with session_factory() as session:
        assert (await session.get(Statement, statement_id)).text == origine
        assert (await session.get(Reformulation, echange.id)).statut is StatutReformulation.refusee
        signalement = await session.scalar(
            select(Signalement).where(Signalement.statement_id == statement_id)
        )
        assert signalement.statut is StatutSignalement.recu


async def test_apres_un_refus_le_responsable_peut_reproposer(
    client, client_factory, moderator_client, session_factory
) -> None:
    """L'index unique est PARTIEL : il n'interdit que deux échanges *en attente*."""
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    await _proposer(moderator_client, statement_id)
    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))
    await client.post(f"/reformulations/{echange.id}/refuser", follow_redirects=False)

    seconde = await _proposer(
        moderator_client, statement_id, texte="Une seconde tentative, plus courte."
    )

    assert seconde.status_code == 303


# --- les gardes ---------------------------------------------------------------------


async def test_un_tiers_ne_peut_pas_repondre_a_la_place_de_l_auteur(
    client, client_factory, moderator_client, session_factory
) -> None:
    """**La garde qui compte le plus de tout le lot.** Sans elle, n'importe qui pourrait
    faire accepter à la place de l'auteur une réécriture de ses propres mots."""
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    async with session_factory() as session:
        origine = (await session.get(Statement, statement_id)).text
    await _proposer(moderator_client, statement_id)
    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))

    async with client_factory() as tiers:
        reponse = await tiers.post(
            f"/reformulations/{echange.id}/accepter", follow_redirects=False
        )
        # Redirige comme un succès : dire « ce n'est pas à vous » confirmerait
        # l'existence de l'échange.
        assert reponse.status_code == 303

    async with session_factory() as session:
        assert (await session.get(Statement, statement_id)).text == origine
        assert (await session.get(Reformulation, echange.id)).statut is StatutReformulation.proposee


async def test_repondre_deux_fois_n_a_aucun_effet(
    client, client_factory, moderator_client, session_factory
) -> None:
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    await _proposer(moderator_client, statement_id)
    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))
    await client.post(f"/reformulations/{echange.id}/accepter", follow_redirects=False)

    await client.post(f"/reformulations/{echange.id}/refuser", follow_redirects=False)

    async with session_factory() as session:
        apres = await session.get(Reformulation, echange.id)
        assert apres.statut is StatutReformulation.acceptee
        actes = list(await session.scalars(select(JournalModeration)))
    assert not [a for a in actes if a.acte is ActeModeration.reformulation_refusee]


async def test_une_proposition_retiree_entre_temps_ne_s_accepte_plus(
    client, client_factory, moderator_client, session_factory
) -> None:
    """La garde qui remplace l'état « caduque » qu'on n'a pas créé : elle se dit au
    moment où l'on essaie, plutôt que de s'écrire dans une colonne que personne ne pose."""
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    await _proposer(moderator_client, statement_id)
    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))
        statement = await session.get(Statement, statement_id)
        origine = statement.text
        statement.moderation_status = ModerationStatus.retire
        await session.commit()

    await client.post(f"/reformulations/{echange.id}/accepter", follow_redirects=False)

    async with session_factory() as session:
        assert (await session.get(Statement, statement_id)).text == origine
        assert (await session.get(Reformulation, echange.id)).statut is StatutReformulation.proposee


async def test_une_reponse_inconnue_rend_404(
    client, client_factory, moderator_client, session_factory
) -> None:
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    await _proposer(moderator_client, statement_id)
    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))

    reponse = await client.post(
        f"/reformulations/{echange.id}/peut-etre", follow_redirects=False
    )
    assert reponse.status_code == 404


# --- ce que l'auteur voit -----------------------------------------------------------


async def test_l_auteur_voit_les_deux_textes_en_regard(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Montrer la reformulation seule demanderait d'accepter de mémoire."""
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    async with session_factory() as session:
        origine = (await session.get(Statement, statement_id)).text
    await _proposer(moderator_client, statement_id)

    page = await client.get("/")

    assert "Une version plus claire de votre proposition vous est proposée" in page.text
    assert origine in page.text
    assert TEXTE_CLAIR in page.text
    assert "Les votes qu'elle a déjà reçus sont conservés" in page.text


async def test_la_navette_remplace_l_avertissement_et_n_offre_pas_de_fermeture(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Deux blocs pour un même texte, dont l'un pose une question et l'autre l'ignore,
    se contrediraient. Et « ne plus afficher » laisserait le responsable attendre une
    réponse qui ne viendrait jamais."""
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    avant = await client.get("/")
    assert "Une de vos propositions est signalée, et reste en ligne" in avant.text

    await _proposer(moderator_client, statement_id)

    page = await client.get("/")
    assert "Une de vos propositions est signalée, et reste en ligne" not in page.text
    assert page.text.count("Une version plus claire") == 1
    assert f"/rappels/reformulation/{statement_id}/fermer" not in page.text


async def test_l_ecran_du_responsable_montre_l_echange_au_lieu_du_formulaire(
    client, client_factory, moderator_client, session_factory
) -> None:
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    avant = await moderator_client.get("/moderation/signalements")
    assert "Proposer une reformulation à l'auteur" in avant.text

    await _proposer(moderator_client, statement_id)

    page = await moderator_client.get("/moderation/signalements")
    assert "Une reformulation attend la réponse de l'auteur" in page.text
    assert "Proposer une reformulation à l'auteur" not in page.text


# --- le registre et l'expiration (MOD-16) -------------------------------------------
#
# Ce que cette section protège : **l'écran des signalements ne montre que ce qu'il y a à
# faire**, et une reformulation acceptée en sort. Sans registre, il ne resterait aucune
# trace lisible de ce qui a été proposé ni de ce que l'auteur a répondu — et le journal
# d'audit, qui la porte, n'est pas un écran de lecture.


async def _vieillir(session_factory, reformulation_id, jours):
    """Recule la date de proposition. L'expiration se règle en jours ; un test qui
    attendrait quinze jours n'en serait pas un."""
    from datetime import datetime, timedelta, timezone

    async with session_factory() as session:
        echange = await session.get(Reformulation, reformulation_id)
        echange.propose_le = datetime.now(timezone.utc) - timedelta(days=jours)
        await session.commit()


async def test_une_reformulation_acceptee_quitte_l_ecran_des_signalements(
    client, client_factory, moderator_client, session_factory
) -> None:
    """**Le « elle disparaît automatiquement » de dom**, et il était déjà vrai : accepter
    clôt la plainte, la plainte close sort de la file, la carte part avec elle. Ce test
    le verrouille — sans lui, rien n'empêcherait un lot futur de laisser traîner la carte."""
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    await _proposer(moderator_client, statement_id)
    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))
    await client.post(f"/reformulations/{echange.id}/accepter", follow_redirects=False)

    page = await moderator_client.get("/moderation/signalements")

    assert "Une reformulation attend la réponse de l'auteur" not in page.text
    assert "Proposer une reformulation à l'auteur" not in page.text
    assert TEXTE_CLAIR not in page.text


async def test_le_registre_garde_les_trois_etats(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Y compris les refusées : un registre qui ne garderait que les succès donnerait à
    lire une suite de réussites, c'est-à-dire le contraire de ce qu'un registre sert à
    faire."""
    _, accepte = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory, titre="Navette acceptée"
    )
    await _proposer(moderator_client, accepte)
    async with session_factory() as session:
        premier = await session.scalar(select(Reformulation))
    await client.post(f"/reformulations/{premier.id}/accepter", follow_redirects=False)

    _, refuse = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory, titre="Navette refusée"
    )
    # Sans apostrophe, délibérément : le gabarit échappe correctement, et une assertion
    # sur un texte apostrophé éprouverait l'échappement au lieu du registre.
    await _proposer(moderator_client, refuse, texte="Une version refusée par son auteur.")
    async with session_factory() as session:
        second = await session.scalar(
            select(Reformulation).where(Reformulation.statement_id == refuse)
        )
    await client.post(f"/reformulations/{second.id}/refuser", follow_redirects=False)

    _, attente = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory, titre="Navette en attente"
    )
    await _proposer(moderator_client, attente, texte="Une version qui attend sa réponse.")

    page = await moderator_client.get("/moderation/reformulations")

    assert page.status_code == 200
    assert "En attente de la réponse de l’auteur" in page.text
    assert "Acceptées par l’auteur" in page.text
    assert "Refusées par l’auteur" in page.text
    assert "Une version refusée par son auteur." in page.text
    assert "Une version qui attend sa réponse." in page.text


async def test_le_registre_ne_porte_aucun_geste(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Les deux réponses possibles appartiennent à l'auteur. Les proposer ici
    reviendrait à laisser le responsable répondre à sa place."""
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    await _proposer(moderator_client, statement_id)

    page = await moderator_client.get("/moderation/reformulations")

    assert "/accepter" not in page.text
    assert "/refuser" not in page.text
    assert "/reformuler" not in page.text


async def test_le_compteur_de_la_barre_suit_les_reformulations_en_attente(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Calculé par la même fonction que la section qu'il annonce — règle du MOD-3a."""
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    await _proposer(moderator_client, statement_id)
    assert 'Reformulations (1)' in (await moderator_client.get("/moderation/")).text

    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))
    await client.post(f"/reformulations/{echange.id}/accepter", follow_redirects=False)

    assert 'Reformulations (1)' not in (await moderator_client.get("/moderation/")).text


async def test_une_reformulation_sans_reponse_disparait_apres_le_delai(
    client, client_factory, moderator_client, session_factory
) -> None:
    """**La règle de dom : quinze jours.** Quatorze ne suffisent pas ; seize effacent."""
    from app.services import reformulation as service

    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    await _proposer(moderator_client, statement_id)
    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))

    await _vieillir(session_factory, echange.id, 14)
    async with session_factory() as session:
        assert await service.purger_expirees(session) == 0

    await _vieillir(session_factory, echange.id, 16)
    async with session_factory() as session:
        assert await service.purger_expirees(session) == 1
        assert await session.get(Reformulation, echange.id) is None


async def test_la_purge_ne_touche_pas_les_reformulations_deja_repondues(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Le délai protège une attente, pas une archive. Une reformulation acceptée il y a
    six mois reste au registre : c'est la seule trace lisible de ce sur quoi les votes
    conservés ont porté."""
    from app.services import reformulation as service

    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    await _proposer(moderator_client, statement_id)
    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))
    await client.post(f"/reformulations/{echange.id}/accepter", follow_redirects=False)
    await _vieillir(session_factory, echange.id, 180)

    async with session_factory() as session:
        assert await service.purger_expirees(session) == 0
        assert await session.get(Reformulation, echange.id) is not None


async def test_la_purge_n_ecrit_rien_au_journal_et_laisse_la_trace_d_origine(
    client, client_factory, moderator_client, session_factory
) -> None:
    """**Ce qui rend la suppression tenable.** Le journal d'audit est en ajout seul : la
    ligne `reformulation_proposee` survit, et l'absence d'un `reformulation_acceptee` en
    face dit qu'elle n'a jamais été validée. On efface un brouillon, pas une trace.

    Et l'expiration elle-même n'est pas journalisée : une échéance qui passe n'est pas un
    acte de modération — la proposition reste exactement ce qu'elle était."""
    from app.services import reformulation as service

    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    await _proposer(moderator_client, statement_id)
    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))
        avant = len(list(await session.scalars(select(JournalModeration))))
    await _vieillir(session_factory, echange.id, 16)

    async with session_factory() as session:
        await service.purger_expirees(session)

    async with session_factory() as session:
        actes = list(await session.scalars(select(JournalModeration)))
    assert len(actes) == avant
    proposees = [a for a in actes if a.acte is ActeModeration.reformulation_proposee]
    assert len(proposees) == 1
    assert proposees[0].cible_id == statement_id


async def test_apres_expiration_le_responsable_peut_reproposer(
    client, client_factory, moderator_client, session_factory
) -> None:
    """C'est le déblocage attendu : l'index unique partiel se libère avec la ligne."""
    from app.services import reformulation as service

    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    await _proposer(moderator_client, statement_id)
    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))
    await _vieillir(session_factory, echange.id, 16)
    async with session_factory() as session:
        await service.purger_expirees(session)

    seconde = await _proposer(
        moderator_client, statement_id, texte="Une seconde tentative, après l'expiration."
    )
    assert seconde.status_code == 303


async def test_apres_expiration_l_auteur_ne_voit_plus_rien_a_valider(
    client, client_factory, moderator_client, session_factory
) -> None:
    """La navette disparaît de son écran en même temps que de la base — et l'avertissement
    simple du MOD-15 reprend sa place, puisque la plainte, elle, est restée ouverte."""
    from app.services import reformulation as service

    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    await _proposer(moderator_client, statement_id)
    assert "Une version plus claire" in (await client.get("/")).text

    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))
    await _vieillir(session_factory, echange.id, 16)
    async with session_factory() as session:
        await service.purger_expirees(session)

    page = await client.get("/")
    assert "Une version plus claire" not in page.text
    assert "Une de vos propositions est signalée, et reste en ligne" in page.text


async def test_le_registre_annonce_le_temps_restant(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Ce qu'on veut savoir d'un échange en attente est quand il va disparaître, pas
    depuis quand il dure."""
    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    await _proposer(moderator_client, statement_id)
    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))
    await _vieillir(session_factory, echange.id, 13)

    page = await moderator_client.get("/moderation/reformulations")

    # Deux jours moins quelques microsecondes : `age_lisible` arrondit vers le bas, et
    # c'est la branche singulière de sa règle qu'on éprouve ici.
    assert "Disparaît dans 1 jour sans réponse" in page.text


async def test_le_worker_passe_bien_par_la_purge(
    client, client_factory, moderator_client, session_factory
) -> None:
    """La purge doit avoir lieu **même sans trafic** : c'est pourquoi elle est au worker
    et non sur un chemin de requête. Ce test vérifie le branchement, pas la règle."""
    from app import worker

    _, statement_id = await _proposition_signalee(
        client, moderator_client, session_factory, client_factory
    )
    await _proposer(moderator_client, statement_id)
    async with session_factory() as session:
        echange = await session.scalar(select(Reformulation))
    await _vieillir(session_factory, echange.id, 16)

    assert await worker.purge_reformulations_expirees() == 1

    async with session_factory() as session:
        assert await session.get(Reformulation, echange.id) is None
