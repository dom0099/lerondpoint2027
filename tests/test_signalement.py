"""Le signalement d'une proposition publiée (chantier Modération, MOD-3a).

Un fichier par sujet, comme le veut la convention du projet. Il couvre les quatre
choses qui peuvent réellement mal tourner, et dans cet ordre d'importance :

  1. **la règle de routage** — dix motifs, dix cas, plus les paires ;
  2. **les fuites d'une proposition retirée** : le tirage pondéré et les API publiques.
     Ce sont les tests qui comptent le plus, parce que c'est là que le retrait pourrait
     ne pas tenir sans que rien ne rougisse ;
  3. **l'ajout seul du journal**, vérifié contre le SERVEUR et pas contre la discipline
     du code ;
  4. **la purge des données de contexte**, qui doit effacer deux colonnes et rien
     d'autre.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select, text, update

from app.models import (
    ActeModeration,
    JournalModeration,
    ModerationStatus,
    Participant,
    Signalement,
    Statement,
    StatutSignalement,
    Vote,
)
from app.services import rate_limit
from app.services import signalement as regles
from app.services import signalement_file
from app.services import votes as votes_service
from app.services.signalement import Route, SignalementInvalide
from tests.conftest import open_conversation


# --- la liste fermée et le routage, sans base ------------------------------------


def test_la_liste_porte_dix_motifs_pas_un_de_plus() -> None:
    """Dix, et dans l'ordre arrêté. L'ordre n'est pas décoratif : `router` s'en sert."""
    assert len(regles.MOTIFS) == 10
    assert regles.CODES == (
        "propos_haineux",
        "incitation_violence",
        "discrimination",
        "donnees_personnelles",
        "fausse_information",
        "source_douteuse",
        "mal_formule",
        "doublon",
        "publicite_spam",
        "autre",
    )
    assert len(set(regles.CODES)) == 10


@pytest.mark.parametrize(
    "code,attendue",
    [
        ("propos_haineux", Route.RETRAIT_CONSERVATOIRE),
        ("incitation_violence", Route.RETRAIT_CONSERVATOIRE),
        ("discrimination", Route.RETRAIT_CONSERVATOIRE),
        ("donnees_personnelles", Route.RETRAIT_CONSERVATOIRE),
        ("fausse_information", Route.A_QUALIFIER),
        ("source_douteuse", Route.A_QUALIFIER),
        ("mal_formule", Route.A_REFORMULER),
        ("doublon", Route.A_FUSIONNER),
        ("publicite_spam", Route.FILE_RESPONSABLE),
        ("autre", Route.FILE_RESPONSABLE),
    ],
)
def test_le_routage_des_dix_motifs(code: str, attendue: Route) -> None:
    """Un cas par motif : c'est le tableau de la consigne, recopié en test."""
    assert regles.router([code]) is attendue


def test_la_gravite_l_emporte_sur_la_frequence() -> None:
    """« En cas de doute, l'item monte, il ne descend pas. »"""
    assert regles.router(["mal_formule", "propos_haineux"]) is Route.RETRAIT_CONSERVATOIRE
    # Et dans l'autre sens : l'ordre de saisie ne décide de rien.
    assert regles.router(["propos_haineux", "mal_formule"]) is Route.RETRAIT_CONSERVATOIRE


def test_la_gravite_tranche_aussi_les_paires_que_la_consigne_ne_nomme_pas() -> None:
    """Sans cette règle, la route dépendrait de l'ordre de saisie, c'est-à-dire du hasard."""
    assert regles.router(["doublon", "fausse_information"]) is Route.A_QUALIFIER
    assert regles.router(["fausse_information", "doublon"]) is Route.A_QUALIFIER


def test_trois_motifs_sont_refuses() -> None:
    """Refus, pas de troncature silencieuse : tronquer choisirait à la place du signaleur."""
    with pytest.raises(SignalementInvalide):
        regles.router(["doublon", "mal_formule", "autre"])


def test_zero_motif_est_refuse() -> None:
    with pytest.raises(SignalementInvalide):
        regles.router([])


def test_un_motif_inconnu_est_refuse() -> None:
    with pytest.raises(SignalementInvalide):
        regles.router(["propos_scandaleux"])


def test_le_meme_motif_deux_fois_est_refuse() -> None:
    """Deux fois la même case n'est pas deux motifs, et dédoublonner en silence
    laisserait croire qu'un second reproche a été enregistré."""
    with pytest.raises(SignalementInvalide):
        regles.router(["doublon", "doublon"])


def test_autre_seul_ne_retire_rien() -> None:
    """Une case libre qui supprime est la case que choisira quiconque veut faire tomber
    une proposition sans avoir à la justifier."""
    assert regles.router(["autre"]) is not Route.RETRAIT_CONSERVATOIRE


def test_fausse_information_seule_ne_retire_rien() -> None:
    """Sur ce site, une affirmation fausse mais inoffensive se qualifie, elle ne se
    supprime pas."""
    assert regles.router(["fausse_information"]) is not Route.RETRAIT_CONSERVATOIRE


def test_autre_accompagne_d_un_motif_doux_ne_retire_toujours_rien() -> None:
    assert regles.router(["autre", "doublon"]) is not Route.RETRAIT_CONSERVATOIRE


def test_seuls_les_quatre_motifs_de_ligne_rouge_retirent() -> None:
    """Le corollaire de tout ce qui précède, énoncé une fois pour toutes."""
    retirants = {m.code for m in regles.MOTIFS if m.route is Route.RETRAIT_CONSERVATOIRE}
    assert retirants == {
        "propos_haineux",
        "incitation_violence",
        "discrimination",
        "donnees_personnelles",
    }
    assert all(regles.est_ligne_rouge([code]) for code in retirants)


# --- le texte libre ---------------------------------------------------------------


def test_le_texte_libre_est_obligatoire_avec_autre() -> None:
    with pytest.raises(SignalementInvalide):
        regles.valider_texte_libre(["autre"], None)
    with pytest.raises(SignalementInvalide):
        regles.valider_texte_libre(["autre"], "   ")


def test_le_texte_libre_est_refuse_avec_tous_les_autres_motifs() -> None:
    with pytest.raises(SignalementInvalide):
        regles.valider_texte_libre(["doublon"], "une explication")


def test_le_texte_libre_est_plafonne_a_cinq_cents_caracteres() -> None:
    assert regles.valider_texte_libre(["autre"], "x" * 500) == "x" * 500
    with pytest.raises(SignalementInvalide):
        regles.valider_texte_libre(["autre"], "x" * 501)


# --- les trois gabarits de message ------------------------------------------------


def test_les_trois_gabarits_portent_le_lien_de_contestation() -> None:
    """Dès qu'un retrait devient visible par son auteur, l'exposé des motifs et la voie
    de recours sont dus — pas au retrait définitif, au premier."""
    for route in Route:
        message = regles.message_a_l_auteur(route, ["propos_haineux"])
        assert regles.CHEMIN_CONTESTATION in message
        assert "réexamen" in message


def test_aucun_message_n_annonce_un_effet_que_le_code_ne_produit_pas() -> None:
    """**La règle du MOD-3b, tenue par un test plutôt que par la vigilance.**

    Elle est née d'un gabarit qui annonçait une proposition « momentanément moins
    montrée » alors que rien n'est déclassé nulle part, et d'un autre qui disait
    « retirée temporairement » pour des routes qui ne retirent rien. Les deux
    promettaient un site qui n'existe pas.

    Seule `RETRAIT_CONSERVATOIRE` retire quelque chose. Aucun autre message ne doit donc
    parler de retrait, ni de diffusion réduite. Le jour où le MOD-12 déclassera
    réellement, c'est ce test qui rappellera de changer le texte AVEC le code.
    """
    interdits = ("retiré", "retirée", "moins montrée", "moins montré", "temporairement")
    for route in Route:
        message = regles.message_a_l_auteur(route, ["doublon"]).lower()
        if route in regles.ROUTES_QUI_RETIRENT:
            assert "retirée" in message
            continue
        for mot in interdits:
            assert mot not in message, f"{route.value} annonce « {mot} »"
        # Et il dit ce qui se passe vraiment : rien ne bouge.
        assert "reste en ligne" in message


def test_le_recours_ne_parle_de_decision_que_lorsqu_il_y_en_a_une() -> None:
    """Le corollaire, sur la phrase commune aux trois : sur deux routes de trois, aucune
    décision n'a été prise — un examen est en cours, c'est tout. Nommer « décision » ce
    qui n'en est pas une ferait croire à un verdict là où il n'y a qu'une plainte."""
    assert "décision" not in regles.RECOURS


def test_chaque_route_a_son_gabarit() -> None:
    assert set(regles.GABARIT_PAR_ROUTE) == set(Route)


def test_le_message_cite_le_motif_qui_a_decide_et_non_le_premier_saisi() -> None:
    """Sans quoi un message dirait « retirée car doublon » pour une proposition retirée
    pour incitation à la violence."""
    message = regles.message_a_l_auteur(
        Route.RETRAIT_CONSERVATOIRE, ["doublon", "incitation_violence"]
    )
    assert "violence" in message
    assert "déjà présente" not in message


def test_le_gabarit_non_rattrapable_ne_promet_aucune_remise_en_ligne() -> None:
    """Un contenu de ligne rouge ne se reformule pas : laisser croire à son auteur
    qu'une retouche le ramènerait serait l'inviter à recommencer."""
    message = regles.message_a_l_auteur(Route.RETRAIT_CONSERVATOIRE, ["propos_haineux"])
    assert "temporairement" not in message
    assert "remise en ligne" not in message
    assert "reste en ligne" not in message


# --- les données de contexte -------------------------------------------------------


def test_le_condense_ne_rend_jamais_la_valeur_en_clair() -> None:
    condense = regles.condenser("203.0.113.7")

    assert condense is not None
    assert "203.0.113.7" not in condense
    assert len(condense) == regles.CONDENSE_LONGUEUR


def test_le_condense_est_stable_et_distingue_deux_origines() -> None:
    """Il ne sert qu'à ça : reconnaître que deux signalements viennent du même endroit."""
    assert regles.condenser("203.0.113.7") == regles.condenser("203.0.113.7")
    assert regles.condenser("203.0.113.7") != regles.condenser("203.0.113.8")


def test_le_condense_d_une_valeur_absente_est_nul() -> None:
    assert regles.condenser(None) is None
    assert regles.condenser("") is None


def test_seul_un_referent_externe_est_conserve() -> None:
    """Un référent interne serait le cas de presque toutes les lignes et ne dirait rien
    d'un afflux coordonné."""
    from app.config import settings

    assert regles.origine_externe(f"{settings.public_base_url}/debats/permis") is None
    assert regles.origine_externe(None) is None
    assert regles.origine_externe("pas une adresse") is None
    assert (
        regles.origine_externe("https://reseau.exemple/fil/123?utm=campagne")
        == "https://reseau.exemple"
    )


def test_le_referent_conserve_ne_porte_jamais_le_chemin() -> None:
    """Le chemin d'un lien partagé peut porter un identifiant de campagne, un pseudonyme,
    une recherche."""
    origine = regles.origine_externe("https://reseau.exemple/u/quelquun/statut/42")

    assert origine == "https://reseau.exemple"
    assert "quelquun" not in origine


# --- le dépôt, en base -------------------------------------------------------------


async def _proposition(moderator_client) -> tuple[int, str, int]:
    """Un débat ouvert et l'identifiant de sa première proposition approuvée."""
    conversation_id, slug, ids = await open_conversation(
        moderator_client, statements=4, title="Débat à signaler"
    )
    return conversation_id, slug, ids[0]


async def _participant(session_factory) -> Participant:
    from app.services import participants as participants_service

    async with session_factory() as session:
        return await participants_service.create_anonymous(session)


async def test_un_visiteur_sans_compte_peut_signaler(
    client, moderator_client, session_factory
) -> None:
    """Le DSA l'impose, et c'est l'identification des votes anonymes qui le permet :
    aucune seconde identification n'a été inventée."""
    _, _, statement_id = await _proposition(moderator_client)

    reponse = await client.post(
        "/api/signalements",
        json={"statement_id": statement_id, "motifs": ["doublon"]},
    )

    assert reponse.status_code == 201, reponse.text
    async with session_factory() as session:
        ligne = await session.scalar(select(Signalement))
        assert ligne is not None
        # Un participant lui a bien été attribué : c'est la ligne anonyme du cookie.
        assert ligne.participant_id is not None
        participant = await session.get(Participant, ligne.participant_id)
        assert participant.user_id is None


async def test_le_signaleur_recoit_toujours_la_meme_reponse(
    client, moderator_client
) -> None:
    """Jamais « rejeté », jamais le sort de la proposition, jamais « déjà signalé ».
    Sinon le signalement devient un jeu où l'on compte les points."""
    _, _, statement_id = await _proposition(moderator_client)

    premiere = await client.post(
        "/api/signalements", json={"statement_id": statement_id, "motifs": ["doublon"]}
    )
    seconde = await client.post(
        "/api/signalements",
        json={"statement_id": statement_id, "motifs": ["propos_haineux"]},
    )

    assert premiere.status_code == seconde.status_code == 201
    assert premiere.json() == seconde.json()


async def test_deux_signalements_de_la_meme_identite_n_en_font_qu_un(
    client, moderator_client, session_factory
) -> None:
    """Idempotence. Portée par une contrainte d'unicité, pas par une vérification
    applicative qui laisserait passer deux requêtes concurrentes."""
    _, _, statement_id = await _proposition(moderator_client)

    for _ in range(3):
        await client.post(
            "/api/signalements",
            json={"statement_id": statement_id, "motifs": ["doublon"]},
        )

    async with session_factory() as session:
        combien = len(list(await session.scalars(select(Signalement))))
    assert combien == 1


async def test_le_second_signalement_ne_change_pas_le_premier(
    client, moderator_client, session_factory
) -> None:
    """Le succès est idempotent, pas « le dernier gagne » : réécrire le motif
    permettrait de transformer après coup un doublon en ligne rouge."""
    _, _, statement_id = await _proposition(moderator_client)

    await client.post(
        "/api/signalements", json={"statement_id": statement_id, "motifs": ["doublon"]}
    )
    await client.post(
        "/api/signalements",
        json={"statement_id": statement_id, "motifs": ["propos_haineux"]},
    )

    async with session_factory() as session:
        ligne = await session.scalar(select(Signalement))
        assert ligne.motif_1 == "doublon"
        statement = await session.get(Statement, statement_id)
        assert statement.moderation_status is ModerationStatus.approved


async def test_deux_identites_distinctes_font_deux_signalements(
    client_factory, moderator_client, session_factory
) -> None:
    _, _, statement_id = await _proposition(moderator_client)

    for _ in range(2):
        async with client_factory() as visiteur:
            await visiteur.post(
                "/api/signalements",
                json={"statement_id": statement_id, "motifs": ["doublon"]},
            )

    async with session_factory() as session:
        combien = len(list(await session.scalars(select(Signalement))))
    assert combien == 2


async def test_le_plafond_horaire_par_identite(
    client, moderator_client, session_factory
) -> None:
    """Dix par heure. Le plafond empêche de balayer un débat entier, pas de signaler."""
    conversation_id, _, _ = await _proposition(moderator_client)
    async with session_factory() as session:
        ids = list(
            await session.scalars(
                select(Statement.id).where(Statement.conversation_id == conversation_id)
            )
        )
    # Il faut plus de propositions que le plafond : le dédoublonnage par proposition
    # ferait sinon que le plafond ne pourrait jamais être atteint.
    for index in range(rate_limit.SIGNALEMENTS_PER_PARTICIPANT + 2 - len(ids)):
        reponse = await moderator_client.post(
            f"/moderation/conversations/{conversation_id}/statements",
            data={"text": f"Proposition de remplissage n°{index}."},
            follow_redirects=False,
        )
        assert reponse.status_code == 303
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

    reponses = []
    for statement_id in ids[: rate_limit.SIGNALEMENTS_PER_PARTICIPANT + 1]:
        reponse = await client.post(
            "/api/signalements",
            json={"statement_id": statement_id, "motifs": ["doublon"]},
        )
        reponses.append(reponse)

    # **Le refus est SILENCIEUX depuis le MOD-3b** : même code, même corps qu'un succès.
    # Le MOD-3a répondait 429 avec le chiffre en clair, ce qui apprenait à qui le lisait
    # qu'un plafond existe et à quelle cadence il se remplit — il ne restait qu'à
    # l'attendre. Ce qui se vérifie n'est donc plus le code de retour, c'est ce qui a
    # réellement atterri en base.
    assert [r.status_code for r in reponses] == [201] * len(reponses)
    assert len({r.text for r in reponses}) == 1

    async with session_factory() as session:
        combien = await session.scalar(select(func.count(Signalement.id)))
    assert combien == rate_limit.SIGNALEMENTS_PER_PARTICIPANT


async def test_trois_motifs_sont_refuses_par_l_api(client, moderator_client) -> None:
    _, _, statement_id = await _proposition(moderator_client)

    reponse = await client.post(
        "/api/signalements",
        json={
            "statement_id": statement_id,
            "motifs": ["doublon", "mal_formule", "autre"],
        },
    )

    assert reponse.status_code == 422


async def test_l_api_expose_la_liste_fermee_sans_sa_route(client) -> None:
    """La route dirait « cette case fait retirer » — l'information qui transforme la
    liste en menu."""
    reponse = await client.get("/api/signalements/motifs")

    assert reponse.status_code == 200
    motifs = reponse.json()
    assert [m["code"] for m in motifs] == list(regles.CODES)
    assert all("route" not in m for m in motifs)
    assert [m["code"] for m in motifs if m["texte_libre_attendu"]] == ["autre"]


# --- la publication directe et son contrôle a posteriori (MOD-14) -------------------


async def test_une_proposition_publiee_d_emblee_reste_signalable(
    client, moderator_client, session_factory
) -> None:
    """**Le contrat du MOD-14, de bout en bout.**

    La pré-modération a disparu : le filet n'est plus devant la publication, il est
    derrière. Ce test vaut donc pour les deux moitiés du nouveau schéma — une proposition
    est publiée sans que personne l'ait relue, ET un signalement de ligne rouge la retire
    aussitôt. Si la seconde moitié tombait, la première deviendrait indéfendable.
    """
    from tests.conftest import open_conversation

    _, slug, _ = await open_conversation(moderator_client, statements=2)

    depot = await client.post(
        f"/api/conversations/{slug}/statements",
        json={"text": "Une proposition déposée sans relecture préalable."},
    )
    assert depot.status_code == 201
    assert depot.json()["moderation_status"] == "approved"

    signalement = await client.post(
        "/api/signalements",
        json={"statement_id": depot.json()["id"], "motifs": ["incitation_violence"]},
    )

    assert signalement.status_code == 201
    async with session_factory() as session:
        statement = await session.get(Statement, depot.json()["id"])
        assert statement.moderation_status is ModerationStatus.retire
        # Et la voie de recours est ouverte du même geste : publier sans relire ne veut
        # pas dire retirer sans recours.
        assert statement.jeton_contestation is not None


# --- le retrait conservatoire ------------------------------------------------------


async def _signaler_ligne_rouge(client, statement_id: int):
    return await client.post(
        "/api/signalements",
        json={"statement_id": statement_id, "motifs": ["incitation_violence"]},
    )


async def test_le_premier_signalement_de_ligne_rouge_retire_sans_seuil(
    client, moderator_client, session_factory
) -> None:
    """Sans seuil, sans délai, sans quorum. Attendre que 20 % des lecteurs signalent un
    appel à la violence, c'est l'avoir laissé tourner."""
    _, _, statement_id = await _proposition(moderator_client)

    await _signaler_ligne_rouge(client, statement_id)

    async with session_factory() as session:
        statement = await session.get(Statement, statement_id)
        assert statement.moderation_status is ModerationStatus.retire
        assert statement.retire_le is not None
        assert statement.retire_motif == "incitation_violence"
        assert statement.retire_par == "systeme"


async def test_une_proposition_retiree_n_est_pas_supprimee_et_garde_ses_votes(
    client, moderator_client, session_factory
) -> None:
    """On ne réécrit pas la mesure du passé."""
    _, slug, statement_id = await _proposition(moderator_client)
    vote = await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": statement_id, "value": 1},
    )
    assert vote.status_code == 200, vote.text

    await _signaler_ligne_rouge(client, statement_id)

    async with session_factory() as session:
        assert await session.get(Statement, statement_id) is not None
        votes = list(
            await session.scalars(
                select(Vote).where(Vote.statement_id == statement_id)
            )
        )
        assert len(votes) == 1


async def test_une_proposition_retiree_ne_sort_plus_du_tirage_pondere(
    client, moderator_client, session_factory
) -> None:
    """**Le test qui compte le plus.** C'est là que se cachent les fuites : le routage
    par priorité du C7 tire parmi les propositions approuvées, et une valeur de statut
    de plus doit suffire à l'en sortir."""
    conversation_id, _, ids = await open_conversation(
        moderator_client, statements=3, title="Débat au tirage"
    )
    retiree = ids[0]

    await _signaler_ligne_rouge(client, retiree)

    from app.models import Conversation
    from app.services import participants as participants_service

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        votant = await participants_service.create_anonymous(session)
        # Deux cents tirages : si la retirée pouvait sortir, elle sortirait.
        for _ in range(200):
            proposee = await votes_service.next_statement(session, conversation, votant)
            assert proposee is not None
            assert proposee.id != retiree
        # Et elle ne compte pas non plus dans ce qu'il reste à voir.
        assert await votes_service.remaining_count(session, conversation, votant) == 2


async def test_une_proposition_retiree_n_apparait_dans_aucune_api_publique(
    client, moderator_client, session_factory
) -> None:
    _, slug, statement_id = await _proposition(moderator_client)
    async with session_factory() as session:
        texte = (await session.get(Statement, statement_id)).text

    await _signaler_ligne_rouge(client, statement_id)

    detail = await client.get(f"/api/conversations/{slug}")
    assert detail.status_code == 200
    assert str(statement_id) not in str(detail.json())
    assert texte not in detail.text

    # Et le parcours de vote ne la sert jamais : `remaining` compte ce qu'il reste à
    # voir, et la retirée n'en fait plus partie.
    suivante = await client.get(f"/api/conversations/{slug}/next-statement")
    assert suivante.status_code == 200
    corps = suivante.json()
    assert corps["remaining"] == 3
    assert corps["statement"]["id"] != statement_id
    assert texte not in suivante.text


async def test_une_proposition_retiree_ne_peut_plus_etre_votee(
    client_factory, moderator_client
) -> None:
    async with client_factory() as signaleur:
        _, slug, statement_id = await _proposition(moderator_client)
        await _signaler_ligne_rouge(signaleur, statement_id)

    async with client_factory() as votant:
        reponse = await votant.post(
            f"/api/conversations/{slug}/votes",
            json={"statement_id": statement_id, "value": 1},
        )

    assert reponse.status_code in (400, 404, 409, 422)


async def test_aucun_signalement_sur_une_proposition_deja_retiree(
    client_factory, moderator_client, session_factory
) -> None:
    """Et le refus est un 404, pas un 409 : un 409 apprendrait au second signaleur que
    la proposition a été retirée, donc que des signalements ont porté."""
    _, _, statement_id = await _proposition(moderator_client)
    async with client_factory() as premier:
        await _signaler_ligne_rouge(premier, statement_id)

    async with client_factory() as second:
        reponse = await second.post(
            "/api/signalements",
            json={"statement_id": statement_id, "motifs": ["doublon"]},
        )

    assert reponse.status_code == 404
    async with session_factory() as session:
        combien = len(list(await session.scalars(select(Signalement))))
    assert combien == 1


async def test_un_motif_doux_ne_retire_rien_en_base(
    client, moderator_client, session_factory
) -> None:
    """Le pendant en base de `test_autre_seul_ne_retire_rien`."""
    _, _, statement_id = await _proposition(moderator_client)

    await client.post(
        "/api/signalements",
        json={
            "statement_id": statement_id,
            "motifs": ["autre"],
            "texte_libre": "Je n'aime pas cette proposition.",
        },
    )

    async with session_factory() as session:
        statement = await session.get(Statement, statement_id)
        assert statement.moderation_status is ModerationStatus.approved
        assert statement.retire_le is None


# --- le journal d'audit, en ajout seul ----------------------------------------------


async def test_le_retrait_automatique_est_journalise(
    client, moderator_client, session_factory
) -> None:
    _, _, statement_id = await _proposition(moderator_client)

    await _signaler_ligne_rouge(client, statement_id)

    async with session_factory() as session:
        lignes = list(await session.scalars(select(JournalModeration)))
    assert len(lignes) == 1
    assert lignes[0].acte is ActeModeration.retrait_conservatoire
    assert lignes[0].cible_type == "statement"
    assert lignes[0].cible_id == statement_id
    assert lignes[0].auteur == "systeme"
    assert lignes[0].motif == "incitation_violence"


async def test_le_journal_refuse_la_modification(
    client, moderator_client, session_factory
) -> None:
    """Vérifié contre le SERVEUR, pas contre la discipline du code : c'est un
    déclencheur PostgreSQL qui refuse, et il refuserait aussi depuis psql."""
    _, _, statement_id = await _proposition(moderator_client)
    await _signaler_ligne_rouge(client, statement_id)

    async with session_factory() as session:
        with pytest.raises(Exception) as leve:
            await session.execute(
                update(JournalModeration).values(auteur="quelqu-un-d-autre")
            )
            await session.commit()
    assert "ajout seul" in str(leve.value)


async def test_le_journal_refuse_la_suppression(
    client, moderator_client, session_factory
) -> None:
    _, _, statement_id = await _proposition(moderator_client)
    await _signaler_ligne_rouge(client, statement_id)

    async with session_factory() as session:
        with pytest.raises(Exception) as leve:
            await session.execute(delete(JournalModeration))
            await session.commit()
    assert "ajout seul" in str(leve.value)


async def test_le_journal_refuse_meme_une_suppression_ciblee(
    client, moderator_client, session_factory
) -> None:
    """Une suppression par identifiant est la forme qu'aurait un ménage bien intentionné."""
    _, _, statement_id = await _proposition(moderator_client)
    await _signaler_ligne_rouge(client, statement_id)

    async with session_factory() as session:
        ligne = await session.scalar(select(JournalModeration))
        with pytest.raises(Exception):
            await session.execute(
                text("DELETE FROM journal_moderation WHERE id = :id"), {"id": ligne.id}
            )
            await session.commit()

    async with session_factory() as session:
        assert len(list(await session.scalars(select(JournalModeration)))) == 1


async def test_le_service_du_journal_n_expose_que_l_ajout_et_la_lecture() -> None:
    """Le pendant statique du déclencheur : rien à appeler pour modifier."""
    from app.services import journal_moderation

    import inspect

    definies = {
        nom
        for nom, objet in inspect.getmembers(journal_moderation, inspect.isfunction)
        if objet.__module__ == journal_moderation.__name__
    }
    assert definies == {"ajouter", "lire"}


# --- l'écran du responsable ---------------------------------------------------------


async def test_l_ecran_groupe_par_proposition(
    client_factory, moderator_client, session_factory
) -> None:
    """Une proposition signalée trois fois est une ligne, pas trois."""
    _, _, statement_id = await _proposition(moderator_client)
    for _ in range(3):
        async with client_factory() as visiteur:
            await visiteur.post(
                "/api/signalements",
                json={"statement_id": statement_id, "motifs": ["doublon"]},
            )

    async with session_factory() as session:
        lignes = await signalement_file.file(session)

    assert len(lignes) == 1
    assert len(lignes[0].signalements) == 3
    assert lignes[0].signaleurs_distincts == 3
    assert lignes[0].motifs_decomptes == [("doublon", 3)]


async def test_l_ecran_trie_par_gravite_puis_par_anciennete(
    client_factory, moderator_client, session_factory
) -> None:
    conversation_id, _, ids = await open_conversation(
        moderator_client, statements=3, title="Débat à trier"
    )
    async with client_factory() as premier:
        await premier.post(
            "/api/signalements", json={"statement_id": ids[0], "motifs": ["doublon"]}
        )
    async with client_factory() as second:
        await second.post(
            "/api/signalements", json={"statement_id": ids[1], "motifs": ["doublon"]}
        )
    async with client_factory() as troisieme:
        await troisieme.post(
            "/api/signalements",
            json={"statement_id": ids[2], "motifs": ["propos_haineux"]},
        )

    async with session_factory() as session:
        lignes = await signalement_file.file(session)

    # La ligne rouge d'abord, bien qu'elle soit la plus récente ; puis les deux
    # doublons, dans l'ordre de leur arrivée.
    assert [ligne.statement.id for ligne in lignes] == [ids[2], ids[0], ids[1]]


async def test_une_proposition_signalee_deux_fois_prend_la_route_la_plus_grave(
    client_factory, moderator_client, session_factory
) -> None:
    _, _, statement_id = await _proposition(moderator_client)
    async with client_factory() as premier:
        await premier.post(
            "/api/signalements",
            json={"statement_id": statement_id, "motifs": ["doublon"]},
        )
    async with client_factory() as second:
        # La proposition n'est pas encore retirée : le doublon ne retire rien.
        await second.post(
            "/api/signalements",
            json={"statement_id": statement_id, "motifs": ["propos_haineux"]},
        )

    async with session_factory() as session:
        lignes = await signalement_file.file(session)

    assert lignes[0].route is Route.RETRAIT_CONSERVATOIRE
    assert lignes[0].retiree is True


async def test_l_ecran_s_affiche_et_montre_le_message_a_l_auteur(
    client, moderator_client
) -> None:
    _, _, statement_id = await _proposition(moderator_client)
    await _signaler_ligne_rouge(client, statement_id)

    page = await moderator_client.get("/moderation/signalements")

    assert page.status_code == 200
    assert "Signalements" in page.text
    assert "retirée car" in page.text
    assert regles.CHEMIN_CONTESTATION in page.text
    # Et la phrase de confidentialité, affichée là où la lira celui qui devra l'ajouter.
    assert "prévention des abus" in page.text


async def test_l_ecran_est_ferme_aux_visiteurs(client) -> None:
    reponse = await client.get("/moderation/signalements", follow_redirects=False)

    assert reponse.status_code == 303
    assert reponse.headers["location"] == "/moderation/login"


async def test_les_deux_reperes_de_la_tete_de_page(
    client, moderator_client, session_factory
) -> None:
    _, _, statement_id = await _proposition(moderator_client)
    await _signaler_ligne_rouge(client, statement_id)

    async with session_factory() as session:
        combien, age = await signalement_file.reperes(session)

    assert combien == 1
    assert age is not None and age < timedelta(minutes=1)
    assert signalement_file.age_lisible(age) == "à l'instant"
    assert signalement_file.age_lisible(None) is None
    assert signalement_file.age_lisible(timedelta(days=2)) == "2 jours"
    assert signalement_file.age_lisible(timedelta(days=1, hours=3)) == "1 jour"
    assert signalement_file.age_lisible(timedelta(hours=5)) == "5 h"


# --- les trois gestes du responsable ------------------------------------------------


async def test_confirmer_le_retrait_laisse_la_proposition_retiree_et_journalise(
    client, moderator_client, session_factory
) -> None:
    _, _, statement_id = await _proposition(moderator_client)
    await _signaler_ligne_rouge(client, statement_id)

    reponse = await moderator_client.post(
        f"/moderation/signalements/{statement_id}/confirmer", follow_redirects=False
    )

    assert reponse.status_code == 303
    async with session_factory() as session:
        statement = await session.get(Statement, statement_id)
        assert statement.moderation_status is ModerationStatus.retire
        signalement = await session.scalar(select(Signalement))
        assert signalement.statut is StatutSignalement.traite
        assert signalement.traite_le is not None
        assert signalement.traite_par is not None
        actes = [ligne.acte for ligne in await session.scalars(select(JournalModeration))]
    assert ActeModeration.retrait_confirme in actes


async def test_annuler_le_retrait_remet_la_proposition_en_circulation(
    client, moderator_client, session_factory
) -> None:
    conversation_id, _, ids = await open_conversation(
        moderator_client, statements=3, title="Débat à rétablir"
    )
    statement_id = ids[0]
    await _signaler_ligne_rouge(client, statement_id)

    await moderator_client.post(
        f"/moderation/signalements/{statement_id}/annuler", follow_redirects=False
    )

    from app.models import Conversation
    from app.services import participants as participants_service

    async with session_factory() as session:
        statement = await session.get(Statement, statement_id)
        assert statement.moderation_status is ModerationStatus.approved
        # Les trois colonnes décrivent un retrait EN COURS, pas un historique :
        # l'historique, c'est le journal, qui garde les deux actes.
        assert statement.retire_le is None
        assert statement.retire_motif is None
        assert statement.retire_par is None

        conversation = await session.get(Conversation, conversation_id)
        votant = await participants_service.create_anonymous(session)
        assert await votes_service.remaining_count(session, conversation, votant) == 3

        actes = [ligne.acte for ligne in await session.scalars(select(JournalModeration))]
    assert actes.count(ActeModeration.retrait_conservatoire) == 1
    assert ActeModeration.retrait_annule in actes


async def test_classer_sans_suite_clot_sans_toucher_a_la_proposition(
    client, moderator_client, session_factory
) -> None:
    _, _, statement_id = await _proposition(moderator_client)
    await client.post(
        "/api/signalements", json={"statement_id": statement_id, "motifs": ["doublon"]}
    )

    await moderator_client.post(
        f"/moderation/signalements/{statement_id}/classer", follow_redirects=False
    )

    async with session_factory() as session:
        statement = await session.get(Statement, statement_id)
        assert statement.moderation_status is ModerationStatus.approved
        signalement = await session.scalar(select(Signalement))
        assert signalement.statut is StatutSignalement.classe_sans_suite
        actes = [ligne.acte for ligne in await session.scalars(select(JournalModeration))]
        assert actes == [ActeModeration.classement_sans_suite]
        # Et la file est vide : un signalement classé n'est plus à traiter.
        assert await signalement_file.file(session) == []


async def test_confirmer_un_retrait_qui_n_a_pas_eu_lieu_est_refuse(
    client, moderator_client
) -> None:
    """Rejouer un lien hors de la file écrirait un acte de journal décrivant quelque
    chose qui n'a pas eu lieu."""
    _, _, statement_id = await _proposition(moderator_client)
    await client.post(
        "/api/signalements", json={"statement_id": statement_id, "motifs": ["doublon"]}
    )

    reponse = await moderator_client.post(
        f"/moderation/signalements/{statement_id}/confirmer", follow_redirects=False
    )

    assert reponse.status_code == 409


async def test_une_decision_inconnue_est_refusee(client, moderator_client) -> None:
    _, _, statement_id = await _proposition(moderator_client)

    reponse = await moderator_client.post(
        f"/moderation/signalements/{statement_id}/supprimer", follow_redirects=False
    )

    assert reponse.status_code == 404


# --- la purge des données de contexte ------------------------------------------------


async def test_la_purge_vide_les_deux_colonnes_de_contexte_et_rien_d_autre(
    client, moderator_client, session_factory
) -> None:
    _, _, statement_id = await _proposition(moderator_client)
    await client.post(
        "/api/signalements",
        json={"statement_id": statement_id, "motifs": ["doublon"]},
        headers={"referer": "https://reseau.exemple/fil/1"},
    )

    async with session_factory() as session:
        avant = await session.scalar(select(Signalement))
        assert avant.ip_hachee is not None
        assert avant.referent_hache is not None
        # On vieillit la ligne de 31 jours : la purge se juge sur une date, et attendre
        # trente jours n'est pas une stratégie de test.
        await session.execute(
            update(Signalement).values(
                cree_le=datetime.now(timezone.utc) - timedelta(days=31)
            )
        )
        await session.commit()

        combien = await signalement_file.purger_contexte(session)

    assert combien == 1
    async with session_factory() as session:
        apres = await session.scalar(select(Signalement))
        assert apres.referent_hache is None
        assert apres.ip_hachee is None
        # Et rien d'autre n'a bougé : le signalement reste, entier.
        assert apres.id == avant.id
        assert apres.motif_1 == avant.motif_1
        assert apres.route == avant.route
        assert apres.statut == avant.statut
        assert apres.participant_id == avant.participant_id


async def test_la_purge_epargne_les_signalements_recents(
    client, moderator_client, session_factory
) -> None:
    _, _, statement_id = await _proposition(moderator_client)
    await client.post(
        "/api/signalements", json={"statement_id": statement_id, "motifs": ["doublon"]}
    )

    async with session_factory() as session:
        combien = await signalement_file.purger_contexte(session)
        ligne = await session.scalar(select(Signalement))

    assert combien == 0
    assert ligne.ip_hachee is not None


async def test_la_purge_ne_recompte_pas_ce_qu_elle_a_deja_vide(
    client, moderator_client, session_factory
) -> None:
    """Deux passages ne doivent pas annoncer deux fois le même travail — c'est le
    chiffre que lira une tâche planifiée."""
    _, _, statement_id = await _proposition(moderator_client)
    await client.post(
        "/api/signalements", json={"statement_id": statement_id, "motifs": ["doublon"]}
    )
    async with session_factory() as session:
        await session.execute(
            update(Signalement).values(
                cree_le=datetime.now(timezone.utc) - timedelta(days=31)
            )
        )
        await session.commit()
        assert await signalement_file.purger_contexte(session) == 1
        assert await signalement_file.purger_contexte(session) == 0


# --- la mesure du rapport --------------------------------------------------------------


async def test_la_mesure_compte_les_motifs_des_deux_colonnes(
    client_factory, moderator_client, session_factory
) -> None:
    _, _, statement_id = await _proposition(moderator_client)
    async with client_factory() as visiteur:
        await visiteur.post(
            "/api/signalements",
            json={"statement_id": statement_id, "motifs": ["doublon", "mal_formule"]},
        )

    async with session_factory() as session:
        m = await signalement_file.mesure(session)

    assert m["total"] == 1
    assert m["par_motif"]["doublon"] == 1
    assert m["par_motif"]["mal_formule"] == 1
    assert m["par_route"][Route.A_REFORMULER] == 1
    assert m["sans_compte"] == 1
    assert m["avec_compte"] == 0
    assert m["plus_signalees"][0][0] == statement_id


async def test_la_mesure_distingue_comptes_et_visiteurs(
    client_factory, moderator_client, session_factory
) -> None:
    from tests.conftest import login, register

    _, _, statement_id = await _proposition(moderator_client)
    async with client_factory() as titulaire:
        await register(titulaire, "signaleur@exemple.fr")
        # S'inscrire ne suffit pas : c'est la SESSION qui décide de l'identité attachée
        # au signalement, exactement comme pour un vote.
        await login(titulaire, "signaleur@exemple.fr")
        await titulaire.post(
            "/api/signalements",
            json={"statement_id": statement_id, "motifs": ["doublon"]},
        )
    async with client_factory() as visiteur:
        await visiteur.post(
            "/api/signalements",
            json={"statement_id": statement_id, "motifs": ["doublon"]},
        )

    async with session_factory() as session:
        m = await signalement_file.mesure(session)

    assert m["avec_compte"] == 1
    assert m["sans_compte"] == 1
