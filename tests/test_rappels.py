"""Le rappel à l'auteur (MOD-6, lot de repli).

Ce que ce fichier protège avant tout : **le flux de vote reste intouchable**. C'est la
troisième fois du chantier qu'on ajoute quelque chose autour de la carte de vote, et la
troisième fois qu'un test le vérifie plutôt que de le supposer.
"""

from sqlalchemy import select

from app.models import (
    ModerationStatus,
    Participant,
    Signalement,
    Statement,
    StatutSignalement,
)
from app.services import rappels as service
from tests.conftest import open_conversation


async def _proposition_d_un_participant(client, moderator_client, session_factory):
    """Un participant dépose une proposition, elle est approuvée, puis retirée."""
    conversation_id, slug, _ = await open_conversation(
        moderator_client, statements=3, title="Débat avec rappel"
    )
    depot = await client.post(
        f"/api/conversations/{slug}/statements",
        json={"text": "Il faudrait revoir entièrement la carte scolaire du département."},
    )
    assert depot.status_code == 201, depot.text
    statement_id = depot.json()["id"]

    # Plus d'approbation à demander : depuis le MOD-14 une proposition naît publiée.
    # L'appel à /moderation/statements/{id}/approve qui figurait ici visait une route
    # supprimée par ce lot — il rendait 404 sans que personne le lise.
    async with session_factory() as session:
        statement = await session.get(Statement, statement_id)
        assert statement.moderation_status is ModerationStatus.approved
    return conversation_id, slug, statement_id


async def _retirer(client_factory, statement_id):
    async with client_factory() as signaleur:
        reponse = await signaleur.post(
            "/api/signalements",
            json={"statement_id": statement_id, "motifs": ["incitation_violence"]},
        )
        assert reponse.status_code == 201


# --- ce que voit, ou ne voit pas, un participant ------------------------------------


async def test_un_participant_sans_rien_en_attente_ne_voit_rien(client) -> None:
    """Le cas courant, et celui qui doit coûter le moins : une requête, et rien à
    montrer."""
    page = await client.get("/")

    assert page.status_code == 200
    assert "Une de vos propositions a été retirée" not in page.text


async def test_l_auteur_d_une_proposition_retiree_voit_l_expose_et_le_recours(
    client, client_factory, moderator_client, session_factory
) -> None:
    """**Le mur que ce lot fait tomber.** L'auteur n'a pas de compte, donc pas d'adresse :
    sans ce rappel, l'exposé des motifs n'atteignait personne."""
    _, _, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _retirer(client_factory, statement_id)

    page = await client.get("/")

    assert "Une de vos propositions a été retirée" in page.text
    assert "retirée car" in page.text
    assert "/contester/" in page.text
    assert "Demander un réexamen" in page.text


async def test_le_rappel_ne_montre_aucun_signalant_ni_aucun_decompte(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Il s'adresse à l'auteur, sur sa propre proposition. Le sort des plaintes ne se
    raconte à personne — c'est la règle du §4 depuis le MOD-3a."""
    _, _, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _retirer(client_factory, statement_id)

    page = await client.get("/")

    assert "signalant" not in page.text.lower()
    assert "signalement" not in page.text.lower()


async def test_un_autre_participant_ne_voit_pas_le_rappel(
    client, client_factory, moderator_client, session_factory
) -> None:
    _, _, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _retirer(client_factory, statement_id)

    async with client_factory() as quelqu_un_d_autre:
        page = await quelqu_un_d_autre.get("/")

    assert "Une de vos propositions a été retirée" not in page.text


# --- la fermeture ---------------------------------------------------------------------


async def test_un_rappel_ferme_ne_revient_pas(
    client, client_factory, moderator_client, session_factory
) -> None:
    _, _, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _retirer(client_factory, statement_id)
    assert "Une de vos propositions" in (await client.get("/")).text

    ferme = await client.post(
        f"/rappels/{statement_id}/fermer", follow_redirects=False
    )

    assert ferme.status_code == 303
    assert "Une de vos propositions" not in (await client.get("/")).text
    assert "Une de vos propositions" not in (await client.get("/debats")).text


async def test_un_tiers_ne_peut_pas_fermer_le_rappel_d_un_autre(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Sans ce contrôle, il suffirait de deviner un identifiant de proposition pour que
    son auteur n'apprenne jamais qu'elle a été retirée."""
    _, _, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _retirer(client_factory, statement_id)

    async with client_factory() as intrus:
        reponse = await intrus.post(
            f"/rappels/{statement_id}/fermer", follow_redirects=False
        )

    assert reponse.status_code in (303, 404)
    # Et l'auteur, lui, le voit toujours.
    assert "Une de vos propositions" in (await client.get("/")).text
    async with session_factory() as session:
        statement = await session.get(Statement, statement_id)
        assert statement.rappel_ferme_le is None


# --- le flux de vote reste intouchable -------------------------------------------------


async def test_le_rappel_n_apparait_jamais_sur_la_page_d_un_debat(
    client, client_factory, moderator_client, session_factory
) -> None:
    """**Le test qui compte le plus.** C'est là qu'on vote, et le flux de vote est resté
    intouchable depuis le MOD-3b : ce n'est pas un rappel didactique qui va l'entamer."""
    _, slug, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _retirer(client_factory, statement_id)

    # Il est bien dû à cet auteur…
    assert "Une de vos propositions" in (await client.get("/")).text
    # …mais la page de vote ne le montre pas.
    debat = await client.get(f"/c/{slug}")

    assert debat.status_code == 200
    assert "Une de vos propositions a été retirée" not in debat.text
    assert "/rappels/" not in debat.text


async def test_le_parcours_de_vote_est_inchange(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Même invariant qu'au MOD-3b : rien n'est consommé, aucun vote n'est posé."""
    _, slug, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _retirer(client_factory, statement_id)

    avant = (await client.get(f"/api/conversations/{slug}/next-statement")).json()
    apres = (await client.get(f"/api/conversations/{slug}/next-statement")).json()

    assert avant["remaining"] == apres["remaining"]
    vote = await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": apres["statement"]["id"], "value": 1},
    )
    assert vote.status_code == 200


async def test_le_service_ne_rend_rien_pour_un_visiteur_sans_identite(
    session_factory,
) -> None:
    async with session_factory() as session:
        assert await service.en_attente(session, None) == []


async def test_une_proposition_remise_en_circulation_ne_laisse_pas_de_rappel(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Le responsable annule le retrait : il n'y a plus rien à annoncer, et le jeton de
    recours est tombé avec lui (MOD-3b)."""
    _, _, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _retirer(client_factory, statement_id)

    await moderator_client.post(
        f"/moderation/signalements/{statement_id}/annuler", follow_redirects=False
    )

    page = await client.get("/")
    assert "Une de vos propositions a été retirée" not in page.text


# --- la seconde forme : le signalement rattrapable (MOD-15) --------------------------
#
# Ce que cette section protège, et qui ne se voit pas dans le code : **le message existe
# depuis le MOD-3b et n'avait aucun destinataire atteignable.** Les tests ci-dessous
# éprouvent l'acheminement, pas la rédaction — aucun texte neuf n'a été écrit au MOD-15.


async def _signaler(client_factory, statement_id, motifs=("mal_formule",)):
    """Un TIERS signale. Jamais l'auteur : un participant ne signale pas son propre texte,
    et la contrainte d'unicité (proposition, identité) ne l'autoriserait qu'une fois."""
    async with client_factory() as signaleur:
        reponse = await signaleur.post(
            "/api/signalements",
            json={"statement_id": statement_id, "motifs": list(motifs)},
        )
        assert reponse.status_code == 201, reponse.text


TETE_REFORMULATION = "Une de vos propositions est signalée, et reste en ligne"
TETE_RETRAIT = "Une de vos propositions a été retirée"


async def test_l_auteur_d_une_proposition_signalee_mal_formulee_est_averti(
    client, client_factory, moderator_client, session_factory
) -> None:
    """**Le mur du MOD-15.** Le texte du gabarit rattrapable existait depuis le MOD-3b,
    relu par le responsable sur son écran, et n'atteignait personne."""
    _, _, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _signaler(client_factory, statement_id)

    page = await client.get("/")

    assert TETE_REFORMULATION in page.text
    assert "elle a été signalée comme mal formulée ou hors sujet" in page.text
    # La moitié de la phrase qui empêche le contresens « signalée » = « retirée ».
    assert "Elle reste en ligne." in page.text
    async with session_factory() as session:
        statement = await session.get(Statement, statement_id)
        assert statement.moderation_status is ModerationStatus.approved


async def test_le_rappel_rattrapable_ne_propose_aucune_porte_qui_n_ouvre_pas(
    client, client_factory, moderator_client, session_factory
) -> None:
    """La voie de recours exige un jeton, posé au seul retrait. Sur une proposition
    encore en ligne il n'y en a pas : proposer « demander un réexamen » enverrait l'auteur
    sur une adresse qui ne mène nulle part."""
    _, _, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _signaler(client_factory, statement_id)

    page = await client.get("/")

    assert TETE_REFORMULATION in page.text
    assert "Demander un réexamen" not in page.text
    assert "/contester" not in page.text


async def test_le_rappel_rattrapable_ramene_au_debat(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Le seul geste proposé, et il n'invente aucune route : le formulaire de proposition
    du débat, ouvert à tous. La navette du MOD-10 n'existe pas et rien ne fait semblant."""
    _, slug, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _signaler(client_factory, statement_id)

    page = await client.get("/")

    assert "Proposer une version plus claire" in page.text
    assert f'href="/c/{slug}"' in page.text


async def test_deux_signalements_sur_la_meme_proposition_font_un_seul_bloc(
    client, client_factory, moderator_client, session_factory
) -> None:
    """La règle du MOD-13 pour les contestations, appliquée ici : un dossier par
    proposition, pas un par plainte. Trois blocs identiques feraient croire à trois
    reproches distincts là où il y a un texte à reprendre.

    Le test COMPTE les occurrences au lieu de chercher une présence : un test qui se
    contenterait de « le bloc est là » resterait vert le jour où il y en aurait trois,
    ce qui est précisément le défaut à empêcher."""
    _, _, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _signaler(client_factory, statement_id)
    await _signaler(client_factory, statement_id, motifs=("doublon",))

    page = await client.get("/")

    assert page.text.count(TETE_REFORMULATION) == 1
    # Les motifs se cumulent, et c'est le plus grave des deux qui est exposé :
    # `mal_formule` précède `doublon` dans la table de gravité.
    assert "mal formulée ou hors sujet" in page.text


async def test_une_route_non_rattrapable_n_avertit_personne(
    client, client_factory, moderator_client, session_factory
) -> None:
    """`fausse_information` part en `A_QUALIFIER` : rien n'est demandé à l'auteur, rien
    ne change pour sa proposition. L'avertir reviendrait à l'inquiéter sans rien lui
    proposer — et la liste des routes qui avertissent est DÉRIVÉE des gabarits, pas
    recopiée, pour qu'elle ne puisse pas diverger."""
    _, _, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _signaler(client_factory, statement_id, motifs=("fausse_information",))

    page = await client.get("/")

    assert TETE_REFORMULATION not in page.text
    assert TETE_RETRAIT not in page.text


async def test_un_signalement_classe_sans_suite_cesse_d_avertir(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Dire « un examen est en cours » sur un dossier clos serait faux."""
    _, _, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _signaler(client_factory, statement_id)
    async with session_factory() as session:
        signalement = await session.scalar(
            select(Signalement).where(Signalement.statement_id == statement_id)
        )
        signalement.statut = StatutSignalement.classe_sans_suite
        await session.commit()

    page = await client.get("/")

    assert TETE_REFORMULATION not in page.text


async def test_le_signaleur_ne_voit_rien_et_ne_peut_rien_fermer(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Le contrôle d'appartenance vaut ici une raison de plus qu'au MOD-6 : le signaleur
    n'est pas l'auteur. Sans lui, celui qui vient de signaler pourrait faire taire
    l'avertissement destiné à celui qu'il signale."""
    _, _, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    async with client_factory() as signaleur:
        depot = await signaleur.post(
            "/api/signalements",
            json={"statement_id": statement_id, "motifs": ["mal_formule"]},
        )
        assert depot.status_code == 201

        page_du_signaleur = await signaleur.get("/")
        assert TETE_REFORMULATION not in page_du_signaleur.text

        await signaleur.post(
            f"/rappels/reformulation/{statement_id}/fermer", follow_redirects=False
        )

    # L'auteur, lui, voit toujours le sien : la tentative du tiers n'a rien éteint.
    assert TETE_REFORMULATION in (await client.get("/")).text


async def test_un_rappel_rattrapable_ferme_ne_revient_pas(
    client, client_factory, moderator_client, session_factory
) -> None:
    _, _, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _signaler(client_factory, statement_id)
    assert TETE_REFORMULATION in (await client.get("/")).text

    ferme = await client.post(
        f"/rappels/reformulation/{statement_id}/fermer", follow_redirects=False
    )

    assert ferme.status_code == 303
    assert TETE_REFORMULATION not in (await client.get("/")).text


async def test_fermer_ecrit_sur_les_signalements_et_non_sur_la_proposition(
    client, client_factory, moderator_client, session_factory
) -> None:
    """**Le test qui justifie la colonne 0023.** Écarter l'avertissement rattrapable ne
    doit pas toucher `statement.rappel_ferme_le`, qui porte l'exposé des motifs d'un
    retrait — lequel est dû au titre du DSA."""
    _, _, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _signaler(client_factory, statement_id)
    await client.post(
        f"/rappels/reformulation/{statement_id}/fermer", follow_redirects=False
    )

    async with session_factory() as session:
        statement = await session.get(Statement, statement_id)
        assert statement.rappel_ferme_le is None
        signalement = await session.scalar(
            select(Signalement).where(Signalement.statement_id == statement_id)
        )
        assert signalement.rappel_ferme_le is not None
        # Le dossier reste `recu` : écarter un rappel n'est pas traiter un signalement.
        assert signalement.statut is StatutSignalement.recu


async def test_un_rappel_rattrapable_ferme_n_eteint_pas_l_expose_d_un_retrait(
    client, client_factory, moderator_client, session_factory
) -> None:
    """**Le défaut que la colonne 0023 évite, joué de bout en bout.** Une proposition
    signalée « mal formulée », dont l'auteur écarte l'avertissement, puis retirée pour
    ligne rouge : l'exposé des motifs du retrait doit lui parvenir. Avec une colonne
    partagée, il ne serait jamais apparu — une commodité de schéma aurait supprimé une
    obligation légale."""
    _, _, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _signaler(client_factory, statement_id)
    await client.post(
        f"/rappels/reformulation/{statement_id}/fermer", follow_redirects=False
    )
    assert TETE_REFORMULATION not in (await client.get("/")).text

    await _retirer(client_factory, statement_id)

    page = await client.get("/")
    assert TETE_RETRAIT in page.text
    assert "Demander un réexamen" in page.text
    # Et pas les deux blocs à la fois : retirée, la proposition sort de la forme
    # rattrapable, dont le message affirmerait « elle reste en ligne ».
    assert TETE_REFORMULATION not in page.text


async def test_un_signalement_deposé_apres_la_fermeture_reparle(
    client, client_factory, moderator_client, session_factory
) -> None:
    """**La seconde raison d'ancrer la fermeture sur le signalement.** Posée sur la
    proposition, elle vaudrait pour toujours : un texte repris, republié, puis signalé de
    nouveau des mois plus tard n'avertirait plus son auteur, parce qu'il avait écarté un
    rappel sans rapport. Chaque plainte neuve reparle."""
    _, _, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _signaler(client_factory, statement_id)
    await client.post(
        f"/rappels/reformulation/{statement_id}/fermer", follow_redirects=False
    )
    assert TETE_REFORMULATION not in (await client.get("/")).text

    await _signaler(client_factory, statement_id, motifs=("doublon",))

    assert TETE_REFORMULATION in (await client.get("/")).text


async def test_le_rappel_rattrapable_n_apparait_jamais_sur_la_page_d_un_debat(
    client, client_factory, moderator_client, session_factory
) -> None:
    """La garde du MOD-6, étendue à la forme neuve. C'est la quatrième fois du chantier
    qu'on ajoute quelque chose autour de la carte de vote, et la quatrième fois qu'un test
    le vérifie plutôt que de le supposer."""
    _, slug, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _signaler(client_factory, statement_id)
    assert TETE_REFORMULATION in (await client.get("/")).text

    debat = await client.get(f"/c/{slug}")

    assert debat.status_code == 200
    assert TETE_REFORMULATION not in debat.text


async def test_fermer_n_ecrit_rien_au_journal_d_audit(
    client, client_factory, moderator_client, session_factory
) -> None:
    """Écarter un rappel n'est pas un acte de modération. Le journal enregistre des actes
    opposables, dans une table en ajout seul qu'on ne peut plus jamais nettoyer : y verser
    un geste de confort mêlerait une trace d'attention à des décisions. C'est la règle
    posée au MOD-13 pour la lecture d'une contestation."""
    from app.models import JournalModeration

    _, _, statement_id = await _proposition_d_un_participant(
        client, moderator_client, session_factory
    )
    await _signaler(client_factory, statement_id)
    async with session_factory() as session:
        avant = len(list(await session.scalars(select(JournalModeration))))

    await client.post(
        f"/rappels/reformulation/{statement_id}/fermer", follow_redirects=False
    )

    async with session_factory() as session:
        apres = len(list(await session.scalars(select(JournalModeration))))
    assert apres == avant
