"""Le bouton de signalement sur la carte de vote (chantier Modération, MOD-3b).

Un fichier séparé de `test_signalement.py`, comme `test_detection_affichage.py` l'est de
`test_detection.py` : là-bas la règle, ici ce qu'on en montre.

**Ce que ces tests peuvent et ne peuvent pas voir.** La suite n'exécute pas de
JavaScript : ce qui est vérifié ici est d'un côté le MARQUAGE servi (le bouton existe, il
est placé avant les réponses, la fenêtre porte les dix motifs, les gardes sont écrites),
et de l'autre l'INVARIANT SERVEUR qui compte vraiment — **signaler ne consomme pas de
proposition et n'enregistre pas de vote**. Ce second point est le test le plus important
du lot, et il ne dépend d'aucun navigateur : si le parcours de vote bougeait, il
bougerait en base, et c'est là qu'on le regarde.
"""

from sqlalchemy import func, select

from app.models import ModerationStatus, Signalement, Statement, Vote
from app.services import rate_limit
from app.services import signalement as regles
from tests.conftest import open_conversation


async def _debat(moderator_client, titre="Débat à signaler"):
    return await open_conversation(moderator_client, statements=4, title=titre)


# --- le marquage de la carte de vote ------------------------------------------------


async def test_le_bouton_est_servi_a_tout_le_monde(client, moderator_client) -> None:
    """Compte ou pas : le DSA ouvre le signalement à tout lecteur, et 164 des 167
    participants du site n'ont pas de compte."""
    _, slug, _ = await _debat(moderator_client)

    page = await client.get(f"/c/{slug}")

    assert page.status_code == 200
    assert 'id="ouvrir-signalement"' in page.text
    assert ">Signaler<" in page.text


async def test_le_bouton_vient_avant_les_reponses_dans_l_ordre_du_document() -> None:
    """**La contrainte principale du lot**, et elle prime sur l'esthétique.

    Le bouton est dans la ligne de méta, en haut de la carte ; les trois réponses sont
    en bas, derrière la boîte à hauteur fixe. Il n'est donc jamais dans l'ordre de
    tabulation ENTRE « D'accord », « Pas d'accord » et « Passer », et sa cible de clic ne
    les jouxte pas.

    Vérifié sur la source du gabarit plutôt que sur une page rendue : c'est l'ordre du
    document qui fait l'ordre de tabulation, et il se lit ici sans ambiguïté.
    """
    import pathlib

    gabarit = pathlib.Path("app/templates/public/conversation.html").read_text()
    bouton = gabarit.index('id="ouvrir-signalement"')
    premiere_reponse = gabarit.index('class="choix-vote" data-vote="1"')
    passer = gabarit.index('class="lien-action" data-vote="0"')

    assert bouton < premiere_reponse < passer


async def test_le_bouton_n_a_aucun_raccourci_clavier() -> None:
    """Les trois réponses affichent leur touche dans un `<kbd>`. Le signalement, non :
    une rafale de votes au clavier ne doit pas pouvoir ouvrir la fenêtre."""
    import pathlib
    import re

    gabarit = pathlib.Path("app/templates/public/conversation.html").read_text()
    ligne = re.search(r'<button[^>]*id="ouvrir-signalement"[^>]*>.*?</button>', gabarit)

    assert ligne is not None
    assert "<kbd" not in ligne.group(0)


async def test_les_raccourcis_de_vote_sont_coupes_quand_la_fenetre_est_ouverte() -> None:
    """Le filtre INPUT/TEXTAREA/SELECT du gestionnaire de touches ne suffit pas : dans la
    fenêtre, le focus se pose aussi sur des BOUTONS, et « espace » y active. Sans cette
    garde, fermer la fenêtre au clavier voterait « Passer » derrière elle."""
    import pathlib

    gabarit = pathlib.Path("app/templates/public/conversation.html").read_text()
    garde = "fenetreSignalement && fenetreSignalement.open"

    assert garde in gabarit
    # Et la garde est AVANT la lecture de la touche, sinon elle ne garde rien.
    assert gabarit.index(garde) < gabarit.index("evenement.key.toLowerCase()")


async def test_la_fenetre_porte_les_dix_motifs_dans_l_ordre_du_module(
    client, moderator_client
) -> None:
    """Dans l'ordre du module, sans regroupement visible par famille : un regroupement
    donnerait au participant une échelle de gravité et orienterait son choix."""
    _, slug, _ = await _debat(moderator_client)

    page = await client.get(f"/c/{slug}")

    from markupsafe import escape

    positions = []
    for motif in regles.MOTIFS:
        marque = f'value="{motif.code}"'
        assert marque in page.text, motif.code
        # Le libellé tel que Jinja l'écrit : les apostrophes des libellés sortent en
        # `&#39;`, et c'est très bien — comparer au texte brut testerait l'absence
        # d'échappement, c'est-à-dire exactement ce qu'on ne veut pas.
        assert str(escape(motif.libelle)) in page.text, motif.code
        positions.append(page.text.index(marque))
    assert positions == sorted(positions)
    # Aucune famille n'est écrite dans la fenêtre.
    for famille in {m.famille.value for m in regles.MOTIFS}:
        assert famille not in page.text


async def test_la_fenetre_ne_montre_jamais_la_route_d_un_motif(
    client, moderator_client
) -> None:
    """Elle dirait « cette case fait retirer », et la liste deviendrait un menu."""
    _, slug, _ = await _debat(moderator_client)

    page = await client.get(f"/c/{slug}")

    for route in regles.Route:
        assert route.value not in page.text


async def test_la_fenetre_est_un_dialog_natif(client, moderator_client) -> None:
    """Le piège à focus, Échap et l'inertie de la page viennent du navigateur : les
    réécrire aurait ajouté un composant là où la plateforme en fournit un correct."""
    _, slug, _ = await _debat(moderator_client)

    page = await client.get(f"/c/{slug}")

    assert '<dialog class="fenetre-signalement"' in page.text
    assert "showModal()" in page.text
    assert 'aria-labelledby="titre-signalement"' in page.text


async def test_le_champ_libre_est_annonce_pour_le_seul_motif_autre(
    client, moderator_client
) -> None:
    _, slug, _ = await _debat(moderator_client)

    page = await client.get(f"/c/{slug}")

    assert 'id="bloc-texte-libre" hidden' in page.text
    assert f'maxlength="{regles.TEXTE_LIBRE_MAX}"' in page.text
    assert f"const MOTIF_TEXTE_LIBRE = {regles.MOTIF_TEXTE_LIBRE!r}".replace(
        "'", '"'
    ) in page.text


# --- l'invariant qui compte : le flux de vote ---------------------------------------


async def test_signaler_ne_consomme_pas_la_proposition_en_cours(
    client, moderator_client, session_factory
) -> None:
    """**Le test qui compte le plus du lot.**

    Signaler n'est pas voter : le reste à voir doit être le MÊME, aucun vote ne doit
    avoir été posé, et la proposition signalée doit rester servable — sinon on éjecte
    quelqu'un de la boucle de vote à chaque signalement, et on perd de la mesure là où
    elle vaut le plus.

    Ce qui n'est PAS affirmé ici : que le second appel rende la même proposition. Il ne
    le ferait pas, et ce serait normal — `next_statement` est un tirage aléatoire
    pondéré (C7), pas une file. C'est d'ailleurs pourquoi la page ne redemande rien après
    un signalement : elle garde la proposition qu'elle a déjà. Voir le test suivant, qui
    vérifie cette absence d'appel dans le gabarit.
    """
    _, slug, _ = await _debat(moderator_client)

    avant = (await client.get(f"/api/conversations/{slug}/next-statement")).json()
    proposee = avant["statement"]["id"]

    reponse = await client.post(
        "/api/signalements", json={"statement_id": proposee, "motifs": ["doublon"]}
    )
    assert reponse.status_code == 201

    apres = (await client.get(f"/api/conversations/{slug}/next-statement")).json()

    assert apres["remaining"] == avant["remaining"]
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Vote.id))) == 0
        # Et elle reste en circulation : un motif doux ne retire rien.
        signalee = await session.get(Statement, proposee)
        assert signalee.moderation_status is ModerationStatus.approved


async def test_l_envoi_d_un_signalement_ne_demande_aucune_proposition_suivante() -> None:
    """Le pendant côté page de l'invariant précédent, et le seul endroit où il se lit.

    Après un envoi réussi, le script ferme la fenêtre et s'arrête là : il n'appelle ni
    `suivante()` ni `voter()`. Un appel à l'un des deux ferait défiler la proposition
    sous les yeux de quelqu'un qui venait seulement de signaler.

    **La borne de fin a été resserrée au MOD-4**, et l'invariant n'a pas bougé. Elle
    allait jusqu'au formulaire de proposition, c'est-à-dire bien au-delà du gestionnaire
    qu'elle décrit ; le bloc de la validation aléatoire, écrit entre les deux, y est
    entré avec ses commentaires — lesquels citent `suivante()` et `voter()` précisément
    pour dire qu'ils ne sont pas appelés non plus. Un test qui lit trois fois plus de
    lignes que son sujet finit par mesurer ses voisins.
    """
    import pathlib

    gabarit = pathlib.Path("app/templates/public/conversation.html").read_text()
    debut = gabarit.index("formSignalement.addEventListener('submit'")
    fin = gabarit.index("} else if (ouvrirSignalement)", debut)
    bloc = gabarit[debut:fin]

    assert "suivante()" not in bloc
    assert "voter(" not in bloc


async def test_le_vote_reste_possible_apres_un_signalement(
    client, moderator_client, session_factory
) -> None:
    """L'abandon comme l'envoi laissent la carte utilisable : le vote en cours n'est
    jamais perdu."""
    _, slug, _ = await _debat(moderator_client)
    servie = (await client.get(f"/api/conversations/{slug}/next-statement")).json()
    proposee = servie["statement"]["id"]

    await client.post(
        "/api/signalements", json={"statement_id": proposee, "motifs": ["doublon"]}
    )
    vote = await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": proposee, "value": 1},
    )

    assert vote.status_code == 200
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Vote.id))) == 1
    # Et le parcours avance normalement ensuite.
    suivante = (await client.get(f"/api/conversations/{slug}/next-statement")).json()
    assert suivante["statement"]["id"] != proposee
    assert suivante["remaining"] == servie["remaining"] - 1


async def test_une_proposition_retiree_n_est_jamais_servie_donc_jamais_signalable(
    client_factory, moderator_client, session_factory
) -> None:
    """« Le bouton n'apparaît pas sur une proposition retirée » : il ne peut pas, la
    carte de vote ne servant jamais une proposition retirée. Vérifié des deux côtés — le
    parcours ne la sert plus, et l'API refuse de la signaler."""
    conversation_id, slug, ids = await _debat(moderator_client, "Débat au retrait")
    retiree = ids[0]
    async with client_factory() as premier:
        await premier.post(
            "/api/signalements",
            json={"statement_id": retiree, "motifs": ["incitation_violence"]},
        )

    async with client_factory() as lecteur:
        for _ in range(30):
            servie = (
                await lecteur.get(f"/api/conversations/{slug}/next-statement")
            ).json()
            if servie["statement"] is None:
                break
            assert servie["statement"]["id"] != retiree
            await lecteur.post(
                f"/api/conversations/{slug}/votes",
                json={"statement_id": servie["statement"]["id"], "value": 0},
            )

        refus = await lecteur.post(
            "/api/signalements", json={"statement_id": retiree, "motifs": ["doublon"]}
        )
    assert refus.status_code == 404
    async with session_factory() as session:
        statement = await session.get(Statement, retiree)
        assert statement.moderation_status is ModerationStatus.retire


# --- les deux plafonds de débit -----------------------------------------------------


async def test_le_plafond_par_adresse_tient_quand_l_identite_change(
    client_factory, moderator_client, session_factory
) -> None:
    """**La raison d'être du second plafond.**

    98 % des participants n'ont pas de compte : l'identité qui porte l'unicité d'un
    signalement est un jeton de session, qui se jette en vidant ses cookies. Un client
    neuf à chaque envoi remet donc le plafond par identité à zéro — mais pas celui de
    l'adresse, qui est la même.
    """
    conversation_id, _, _ = await _debat(moderator_client, "Débat à saturer")
    besoin = rate_limit.SIGNALEMENTS_PER_ADRESSE + 2
    for index in range(besoin):
        await moderator_client.post(
            f"/moderation/conversations/{conversation_id}/statements",
            data={"text": f"Proposition de remplissage n°{index}."},
            follow_redirects=False,
        )
    async with session_factory() as session:
        ids = list(
            await session.scalars(
                select(Statement.id)
                .where(
                    Statement.conversation_id == conversation_id,
                    Statement.moderation_status == ModerationStatus.approved,
                )
                .order_by(Statement.id)
            )
        )

    codes = []
    for statement_id in ids[:besoin]:
        # Un bocal à cookies neuf par envoi : une identité neuve à chaque fois.
        async with client_factory() as visiteur:
            reponse = await visiteur.post(
                "/api/signalements",
                json={"statement_id": statement_id, "motifs": ["doublon"]},
            )
            codes.append(reponse.status_code)

    # Toujours 201 : le refus est silencieux. C'est le décompte en base qui parle.
    assert set(codes) == {201}
    async with session_factory() as session:
        combien = await session.scalar(select(func.count(Signalement.id)))
    assert combien == rate_limit.SIGNALEMENTS_PER_ADRESSE


async def test_les_deux_plafonds_sont_deux_seaux_distincts(session_factory) -> None:
    """Deux plafonds, deux seaux : un seul compteur partagé ferait qu'une identité très
    active fermerait la porte à son voisin de palier, et réciproquement."""
    from app.models import RateLimitHit

    async with session_factory() as session:
        assert await rate_limit.signalement_allowed(session, 1, "condense-a") is True
        seaux = set(await session.scalars(select(RateLimitHit.bucket)))

    assert seaux == {"signalement:1", "signalement-adresse:condense-a"}


async def test_le_plafond_par_adresse_ne_stocke_jamais_l_adresse(
    client, moderator_client, session_factory
) -> None:
    """Le seau porte le CONDENSÉ, celui-là même qui va en base — jamais l'adresse.

    Les seaux plus anciens du fichier (`reset-ip:`, `conv-ip:`) portent l'adresse en
    clair : c'est une dette de leur époque, pas un modèle à suivre.
    """
    from app.models import RateLimitHit

    conversation_id, slug, ids = await _debat(moderator_client, "Débat au condensé")
    await client.post(
        "/api/signalements", json={"statement_id": ids[0], "motifs": ["doublon"]}
    )

    async with session_factory() as session:
        seaux = [
            seau
            for seau in await session.scalars(select(RateLimitHit.bucket))
            if seau.startswith("signalement-adresse:")
        ]

    assert seaux, "aucun seau d'adresse écrit"
    for seau in seaux:
        condense = seau.split(":", 1)[1]
        assert len(condense) == regles.CONDENSE_LONGUEUR
        assert "." not in condense and ":" not in condense
