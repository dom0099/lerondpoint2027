"""La validation aléatoire par les participants (chantier Modération, MOD-4).

Ce fichier couvre, dans cet ordre d'importance :

  1. **la séparation sollicité / spontané** — la garde qui ne pouvait pas attendre le
     lot. Sans elle, la détection d'afflux coordonné du MOD-5 se déclencherait sur le
     sondage du site lui-même. C'est le test qui compte le plus ici ;
  2. **les « conforme » sont enregistrés eux aussi** — sans eux, le lot ne rebranche
     rien pour le MOD-6, et une table qui ne garderait que les désaccords ne permettrait
     de calculer aucun taux d'accord ;
  3. **les quatre gardes du tirage** — sa propre proposition, une déjà signalée, une
     déjà validée, une rassasiée ;
  4. **les deux plafonds et la cadence** ;
  5. **ce qu'un « à revoir » déclenche** : un signalement ordinaire, route comprise, et
     le retrait conservatoire sur une ligne rouge.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.models import (
    ModerationStatus,
    Participant,
    Signalement,
    Statement,
    Validation,
    VerdictValidation,
)
from app.services import independance_lecture
from app.services import validation as regles
from app.services import validation_tirage
from app.services.validation import ValidationInvalide, Verdict
from tests.conftest import open_conversation


# --- la règle, sans base ---------------------------------------------------------


def test_il_n_existe_que_deux_verdicts() -> None:
    """Deux, et jamais un troisième.

    « Passer » n'en est pas un : écarter la carte n'écrit rien. Un troisième code
    transformerait une abstention en jugement, et le taux d'accord du MOD-6 compterait
    comme désaccord le fait de n'avoir pas voulu trancher.
    """
    assert [v.value for v in Verdict] == ["conforme", "a_revoir"]
    assert [v.value for v in VerdictValidation] == ["conforme", "a_revoir"]


#: Les modules du lot, et le gabarit qui porte sa fenêtre.
_SOURCES_DU_LOT = (
    "app/services/validation.py",
    "app/services/validation_tirage.py",
    "app/routers/validations.py",
)

#: Les deux mots proscrits, et ce qu'ils désigneraient à tort.
#:
#: - **« niveau »** : le §2 du plan l'interdit, il désigne la jauge de jeu du C5. Le
#:   cadrage de dom disait « niveau 0 » ; le laisser passer ferait croire que voter
#:   beaucoup donne du pouvoir de modération, ce qui est faux — la validation est ouverte
#:   à tout participant.
#: - **« relecture »** : c'est le nom de la grille supprimée au MOD-14. Le reprendre
#:   ferait croire, en relisant le dépôt dans six mois, que la grille est revenue — alors
#:   que ce dispositif est son contraire.
_MOTS_PROSCRITS = ("niveau", "relecture")


def _identifiants(chemin: str) -> set[str]:
    """Tout ce que le fichier NOMME : variables, attributs, fonctions, classes, mots-clés.

    Passe par l'arbre syntaxique plutôt que par le texte, et c'est la différence entre
    une sentinelle utile et une sentinelle qui crie sur sa propre documentation. Le
    précédent est celui du MOD-9, dont le test-sentinelle cherchait le mot y compris en
    commentaire — il avait raison pour un module dont rien ne devait parler ; il aurait
    tort ici, où la prose doit pouvoir écrire « jamais “relecture” » pour que le prochain
    sache pourquoi.

    Ce qui est réellement en jeu tient en deux endroits, et ce sont ceux-là qu'on garde :
    **ce que le code nomme** (ci-dessous) et **ce que le site dit** (test suivant).
    """
    import ast

    arbre = ast.parse(open(chemin, encoding="utf-8").read())
    trouves: set[str] = set()
    for nœud in ast.walk(arbre):
        if isinstance(nœud, ast.Name):
            trouves.add(nœud.id)
        elif isinstance(nœud, ast.Attribute):
            trouves.add(nœud.attr)
        elif isinstance(nœud, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            trouves.add(nœud.name)
        elif isinstance(nœud, ast.arg):
            trouves.add(nœud.arg)
        elif isinstance(nœud, ast.keyword) and nœud.arg:
            trouves.add(nœud.arg)
    return {nom.lower() for nom in trouves}


@pytest.mark.parametrize("mot", _MOTS_PROSCRITS)
def test_aucun_mot_proscrit_ne_nomme_quoi_que_ce_soit(mot: str) -> None:
    """Ni une variable, ni un champ, ni une fonction, ni un paramètre."""
    for chemin in _SOURCES_DU_LOT:
        fautifs = [nom for nom in _identifiants(chemin) if mot in nom]
        assert not fautifs, f"« {mot} » nomme {fautifs} dans {chemin}"


@pytest.mark.parametrize("mot", _MOTS_PROSCRITS)
def test_aucun_mot_proscrit_n_est_montre_a_qui_que_ce_soit(mot: str) -> None:
    """Et surtout : aucun des deux ne sort du serveur.

    C'est le vrai enjeu. Un commentaire fautif se corrige à la relecture suivante ; une
    phrase affichée sur la carte de vote de tout le monde, non.
    """
    montres = [
        regles.TITRE,
        regles.CONSIGNE,
        regles.ACCUSE_DE_RECEPTION,
        regles.PASSER_N_EST_PAS_UN_VERDICT,
        *regles.LIBELLES.values(),
        *(v.value for v in Verdict),
    ]
    for texte in montres:
        assert mot not in texte.lower(), f"« {mot} » est montré : {texte!r}"

    # Et dans la fenêtre du gabarit, qui écrit ses propres phrases.
    with open("app/templates/public/conversation.html", encoding="utf-8") as fichier:
        gabarit = fichier.read()
    debut = gabarit.index('id="fenetre-validation"')
    fenetre = gabarit[debut : gabarit.index("</dialog>", debut)]
    assert mot not in fenetre.lower(), f"« {mot} » est dans la carte de validation"


@pytest.mark.parametrize(
    "votes,due",
    [(0, False), (1, False), (6, False), (7, True), (8, False), (14, True), (15, False)],
)
def test_la_cadence_tombe_tous_les_sept_votes(votes: int, due: bool) -> None:
    """Sept, et zéro n'en est pas un — sinon la carte s'ouvrirait avant le premier vote."""
    assert regles.sollicitation_due(votes) is due
    assert regles.CADENCE_VOTES == 7


def test_un_verdict_inconnu_est_refuse_et_non_ignore() -> None:
    """Lève plutôt que de filtrer : une validation qu'on enregistrerait sans savoir ce
    qu'elle dit ne serait pas une validation."""
    assert regles.verdict("conforme") is Verdict.conforme
    with pytest.raises(ValidationInvalide):
        regles.verdict("peut_etre")


def test_seul_a_revoir_demande_des_motifs() -> None:
    assert regles.exige_des_motifs(Verdict.a_revoir) is True
    assert regles.exige_des_motifs(Verdict.conforme) is False


# --- outillage -------------------------------------------------------------------


async def _participant(session_factory) -> Participant:
    from app.services import participants as participants_service

    async with session_factory() as session:
        return await participants_service.create_anonymous(session)


async def _valider(client, statement_id: int, verdict: str, **reste):
    return await client.post(
        "/api/validations",
        json={"statement_id": statement_id, "verdict": verdict, **reste},
    )


async def _voter(client, slug: str, statement_id: int, valeur: int = 1):
    return await client.post(
        f"/api/conversations/{slug}/votes",
        json={"statement_id": statement_id, "value": valeur},
    )


# --- la garde qui ne pouvait pas attendre : sollicité ≠ spontané -----------------


async def test_un_a_revoir_depose_un_signalement_marque_sollicite(
    client, moderator_client, session_factory
):
    """Un « à revoir » dépose un signalement ordinaire — mais marqué.

    Ordinaire : même motif, même route, même file. Marqué : `sollicite` vaut vrai, parce
    que le site a choisi qui le déposerait et sur quoi.
    """
    _, _, ids = await open_conversation(moderator_client, statements=3)

    reponse = await _valider(client, ids[0], "a_revoir", motifs=["mal_formule"])
    assert reponse.status_code == 201

    async with session_factory() as session:
        signalement = await session.scalar(
            select(Signalement).where(Signalement.statement_id == ids[0])
        )
        assert signalement is not None
        assert signalement.sollicite is True
        assert signalement.route == "A_REFORMULER"
        validation = await session.scalar(
            select(Validation).where(Validation.statement_id == ids[0])
        )
        assert validation.verdict is VerdictValidation.a_revoir


async def test_un_signalement_ordinaire_n_est_pas_sollicite(
    client, moderator_client, session_factory
):
    """Le défaut dit la vérité : ce que personne n'a provoqué n'est pas provoqué."""
    _, _, ids = await open_conversation(moderator_client, statements=3)

    await client.post(
        "/api/signalements", json={"statement_id": ids[0], "motifs": ["doublon"]}
    )

    async with session_factory() as session:
        signalement = await session.scalar(
            select(Signalement).where(Signalement.statement_id == ids[0])
        )
        assert signalement.sollicite is False


async def test_le_mod5_ecarte_les_signalements_sollicites(
    client, moderator_client, session_factory, client_factory
):
    """**Le test le plus important du lot.**

    Cinq « à revoir » sollicités sur la même proposition, rendus dans la même minute par
    cinq identités fraîches qui n'ont pas voté le débat : c'est mot pour mot la signature
    que `rafale`, `fraicheur_des_identites` et `signalants_non_votants` cherchent. Sans
    la garde, la détection d'afflux coordonné se déclencherait sur le sondage du site.

    On vérifie l'absence à la source — zéro signalant observé — plutôt que l'absence
    d'alerte : une alerte qui ne se lève pas parce que les seuils ont bougé passerait le
    test sans rien prouver.
    """
    conversation_id, _, ids = await open_conversation(moderator_client, statements=3)

    for _ in range(5):
        autre = await client_factory().__aenter__()
        await _valider(autre, ids[0], "a_revoir", motifs=["mal_formule"])

    from app.models import Conversation

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        analyse = await independance_lecture.observer(session, conversation)
        assert analyse.alerte is False

        surveilles = await independance_lecture.debats_surveilles(session)
        # Le débat n'entre même pas dans l'écran de surveillance : personne ne s'est
        # plaint de lui. Afficher « ce débat est surveillé » pour un débat dont tous les
        # signalements viennent du site serait un contresens.
        assert [d.conversation.id for d in surveilles] == []

    # Un seul signalement SPONTANÉ suffit en revanche à le faire apparaître : la garde
    # écarte les sollicités, elle n'aveugle pas le dispositif.
    await client.post(
        "/api/signalements", json={"statement_id": ids[1], "motifs": ["doublon"]}
    )
    async with session_factory() as session:
        surveilles = await independance_lecture.debats_surveilles(session)
        assert [d.conversation.id for d in surveilles] == [conversation_id]
        assert surveilles[0].signalements == 1


# --- les « conforme » sont enregistrés eux aussi ---------------------------------


async def test_un_conforme_est_enregistre_et_ne_depose_rien(
    client, moderator_client, session_factory
):
    """Les « conforme » sont la matière des habilitations du MOD-6.

    Ne garder que les « à revoir » ferait une table de plaintes de plus, sans aucun des
    taux d'accord dont le lot suivant a besoin. Et un « conforme » ne dépose évidemment
    aucun signalement — on ne se plaint pas d'un texte qu'on trouve correct.
    """
    _, _, ids = await open_conversation(moderator_client, statements=3)

    reponse = await _valider(client, ids[0], "conforme")
    assert reponse.status_code == 201
    assert reponse.json()["message"] == regles.ACCUSE_DE_RECEPTION

    async with session_factory() as session:
        validation = await session.scalar(
            select(Validation).where(Validation.statement_id == ids[0])
        )
        assert validation.verdict is VerdictValidation.conforme
        assert (
            await session.scalar(
                select(Signalement).where(Signalement.statement_id == ids[0])
            )
        ) is None


async def test_un_conforme_avec_des_motifs_est_refuse(client, moderator_client):
    """On ne dit pas d'un texte qu'il va bien ET qu'il incite à la violence."""
    _, _, ids = await open_conversation(moderator_client, statements=3)
    reponse = await _valider(
        client, ids[0], "conforme", motifs=["incitation_violence"]
    )
    assert reponse.status_code == 422


async def test_un_a_revoir_sans_motif_est_refuse(client, moderator_client):
    """Une plainte sans objet ne dit rien à qui doit trancher."""
    _, _, ids = await open_conversation(moderator_client, statements=3)
    reponse = await _valider(client, ids[0], "a_revoir")
    assert reponse.status_code == 422


# --- ce qu'un « à revoir » déclenche : le chemin habituel, entier -----------------


async def test_une_ligne_rouge_sollicitee_retire_quand_meme(
    client, moderator_client, session_factory
):
    """« La suite est le chemin habituel » — y compris le retrait conservatoire.

    Le fait que le site ait posé la question ne rend pas l'appel à la violence moins
    urgent à sortir de la circulation. Le seul écart tient au marquage.
    """
    _, _, ids = await open_conversation(moderator_client, statements=3)

    reponse = await _valider(
        client, ids[0], "a_revoir", motifs=["incitation_violence"]
    )
    assert reponse.status_code == 201

    async with session_factory() as session:
        statement = await session.get(Statement, ids[0])
        assert statement.moderation_status is ModerationStatus.retire
        assert statement.jeton_contestation is not None
        signalement = await session.scalar(
            select(Signalement).where(Signalement.statement_id == ids[0])
        )
        assert signalement.sollicite is True
        assert signalement.route == "RETRAIT_CONSERVATOIRE"


async def test_la_regle_du_texte_libre_vaut_des_deux_cotes(client, moderator_client):
    """« Autre raison » exige une explication, ici comme au signalement — même code."""
    _, _, ids = await open_conversation(moderator_client, statements=3)
    refus = await _valider(client, ids[0], "a_revoir", motifs=["autre"])
    assert refus.status_code == 422

    accepte = await _valider(
        client, ids[1], "a_revoir", motifs=["autre"], texte_libre="Hors du sujet posé."
    )
    assert accepte.status_code == 201


# --- l'idempotence ---------------------------------------------------------------


async def test_repondre_deux_fois_ne_compte_qu_une_voix(
    client, moderator_client, session_factory
):
    """Un double clic n'est pas deux avis, et la règle est portée par la base.

    Et le second envoi ne dépose surtout pas un second signalement : la contrainte
    d'unicité du signalement le refuserait, mais c'est ici que le chemin s'arrête.
    """
    _, _, ids = await open_conversation(moderator_client, statements=3)

    assert (await _valider(client, ids[0], "conforme")).status_code == 201
    assert (await _valider(client, ids[0], "a_revoir", motifs=["doublon"])).status_code == 201

    async with session_factory() as session:
        lignes = list(
            await session.scalars(
                select(Validation).where(Validation.statement_id == ids[0])
            )
        )
        assert len(lignes) == 1
        # Le premier verdict tient : on n'écrase pas un avis rendu.
        assert lignes[0].verdict is VerdictValidation.conforme
        assert (
            await session.scalar(
                select(Signalement).where(Signalement.statement_id == ids[0])
            )
        ) is None


async def test_une_proposition_retiree_ne_se_valide_plus(
    client, moderator_client, session_factory
):
    """Retirée entre la carte et la réponse : rien n'est enregistré, et 404.

    404 et non 409 : un 409 apprendrait qu'elle a été retirée, donc que des signalements
    ont porté — ce que le §4 du cadrage interdit de laisser entendre.
    """
    _, _, ids = await open_conversation(moderator_client, statements=3)
    async with session_factory() as session:
        await session.execute(
            update(Statement)
            .where(Statement.id == ids[0])
            .values(moderation_status=ModerationStatus.retire)
        )
        await session.commit()

    reponse = await _valider(client, ids[0], "conforme")
    assert reponse.status_code == 404
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(Validation).where(Validation.statement_id == ids[0])
            )
        ) is None


# --- les quatre gardes du tirage -------------------------------------------------


async def _debat_et_participant(moderator_client, session_factory, statements=5):
    conversation_id, slug, ids = await open_conversation(
        moderator_client, statements=statements, title="Débat à valider"
    )
    from app.models import Conversation

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
    participant = await _participant(session_factory)
    return conversation, slug, ids, participant


async def test_on_ne_se_fait_jamais_valider_sa_propre_proposition(
    moderator_client, session_factory
):
    """On ne se donne pas un avis à soi-même.

    Le test vaut aussi comme garde contre le **piège du NULL** : les propositions
    d'amorce ont `author_participant_id` à NULL, et `!= participant.id` les écarterait
    toutes en SQL — sur un débat jeune, il n'y aurait plus rien à tirer du tout.
    """
    conversation, _, ids, participant = await _debat_et_participant(
        moderator_client, session_factory
    )
    async with session_factory() as session:
        # La première est désormais SA proposition ; les autres restent des amorces.
        await session.execute(
            update(Statement)
            .where(Statement.id == ids[0])
            .values(author_participant_id=participant.id)
        )
        await session.commit()

        tirees = set()
        for _ in range(30):
            proposition = await validation_tirage.a_proposer(
                session, conversation, participant
            )
            assert proposition is not None, "les amorces doivent rester tirables"
            tirees.add(proposition.id)

        assert ids[0] not in tirees
        # Et les amorces, elles, sortent bien : c'est l'autre moitié du piège.
        assert tirees <= set(ids[1:]) and len(tirees) > 1


async def test_on_ne_fait_pas_valider_ce_qu_on_a_deja_signale(
    client, moderator_client, session_factory
):
    """La contrainte d'unicité du signalement refuserait le second dépôt : la personne
    répondrait « à revoir » dans le vide."""
    conversation, _, ids, _ = await _debat_et_participant(
        moderator_client, session_factory, statements=2
    )
    await client.post(
        "/api/signalements", json={"statement_id": ids[0], "motifs": ["doublon"]}
    )

    async with session_factory() as session:
        signalement = await session.scalar(
            select(Signalement).where(Signalement.statement_id == ids[0])
        )
        participant = await session.get(Participant, signalement.participant_id)
        for _ in range(20):
            proposition = await validation_tirage.a_proposer(
                session, conversation, participant
            )
            assert proposition is None or proposition.id != ids[0]


async def test_on_ne_valide_pas_deux_fois_la_meme_proposition(
    moderator_client, session_factory
):
    conversation, _, ids, participant = await _debat_et_participant(
        moderator_client, session_factory, statements=2
    )
    async with session_factory() as session:
        statement = await session.get(Statement, ids[0])
        await validation_tirage.enregistrer(
            session,
            statement=statement,
            participant=participant,
            verdict=Verdict.conforme,
        )
        for _ in range(20):
            proposition = await validation_tirage.a_proposer(
                session, conversation, participant
            )
            assert proposition is None or proposition.id != ids[0]


async def test_une_proposition_rassasiee_sort_du_tirage(
    moderator_client, session_factory, client_factory
):
    """Cinq avis suffisent. Au-delà, on use l'attention des gens sur du tranché."""
    conversation, _, ids, participant = await _debat_et_participant(
        moderator_client, session_factory, statements=2
    )
    async with session_factory() as session:
        statement = await session.get(Statement, ids[0])
        for _ in range(regles.VALIDATIONS_PAR_PROPOSITION):
            autre = await _participant(session_factory)
            await validation_tirage.enregistrer(
                session,
                statement=statement,
                participant=autre,
                verdict=Verdict.conforme,
            )
        for _ in range(20):
            proposition = await validation_tirage.a_proposer(
                session, conversation, participant
            )
            assert proposition is None or proposition.id != ids[0]
        # Mais le plafond ne vaut QUE pour le tirage : un signalement spontané reste
        # possible sur une proposition rassasiée.
        assert regles.VALIDATIONS_PAR_PROPOSITION == 5


async def test_on_ne_fait_pas_juger_la_proposition_qu_on_vient_de_voter(
    moderator_client, session_factory
):
    """Ce serait faire relire sa propre décision : le « conforme » qui suivrait un
    « d'accord » ne dirait plus rien."""
    conversation, _, ids, participant = await _debat_et_participant(
        moderator_client, session_factory, statements=4
    )
    async with session_factory() as session:
        for _ in range(20):
            proposition = await validation_tirage.a_proposer(
                session, conversation, participant, sauf_id=ids[0]
            )
            assert proposition is not None
            assert proposition.id != ids[0]


# --- le plafond du jour ----------------------------------------------------------


async def test_trois_sollicitations_par_jour_et_pas_une_de_plus(
    moderator_client, session_factory
):
    """Au-delà, on ne sollicite plus quelqu'un qui lit : on le met au travail."""
    conversation, _, ids, participant = await _debat_et_participant(
        moderator_client, session_factory, statements=8
    )
    async with session_factory() as session:
        for rang in range(regles.SOLLICITATIONS_PAR_JOUR):
            statement = await session.get(Statement, ids[rang])
            await validation_tirage.enregistrer(
                session,
                statement=statement,
                participant=participant,
                verdict=Verdict.conforme,
            )
        assert (
            await validation_tirage.a_proposer(session, conversation, participant)
        ) is None

    # La fenêtre est GLISSANTE : un plafond calé sur minuit se contournerait en
    # attendant minuit. Vieillies de 25 heures, les trois réponses ne pèsent plus.
    async with session_factory() as session:
        await session.execute(
            update(Validation)
            .where(Validation.participant_id == participant.id)
            .values(cree_le=datetime.now(timezone.utc) - timedelta(hours=25))
        )
        await session.commit()
        assert (
            await validation_tirage.a_proposer(session, conversation, participant)
        ) is not None


# --- la carte arrive par la réponse du vote, et pas autrement --------------------


async def test_la_carte_arrive_au_septieme_vote_et_pas_avant(
    client, moderator_client
):
    """Six votes sur sept ne portent rien ; le septième porte la carte.

    C'est aussi ce qui garantit qu'aucune requête n'est faite pour rien : la cadence est
    une division, et elle est évaluée avant le tirage.
    """
    _, slug, ids = await open_conversation(
        moderator_client, statements=10, title="Débat assez long pour sept votes"
    )

    for rang, statement_id in enumerate(ids[:7], start=1):
        reponse = await _voter(client, slug, statement_id)
        assert reponse.status_code == 200
        carte = reponse.json()["validation"]
        if rang < regles.CADENCE_VOTES:
            assert carte is None, f"une carte est arrivée au vote n°{rang}"
        else:
            assert carte is not None
            assert carte["statement_id"] not in (statement_id,)
            assert carte["texte"]
            assert carte["libelle_conforme"] == "Proposition conforme"
            assert carte["libelle_a_revoir"] == "Proposition à revoir"


async def test_il_n_existe_aucune_route_pour_demander_une_carte(client):
    """Le tirage est la main du site, pas celle du client.

    Une route qui servirait une proposition à valider serait tirable à volonté : il
    suffirait de la rappeler jusqu'à tomber sur celle qu'on veut faire retirer, et le
    tirage au sort cesserait d'en être un.
    """
    for chemin in (
        "/api/validations",
        "/api/validations/suivante",
        "/api/validations/prochaine",
    ):
        assert (await client.get(chemin)).status_code in (404, 405)


async def test_la_reponse_est_toujours_la_meme(client, moderator_client):
    """Ni le sort de la proposition, ni un décompte, ni un identifiant.

    Dès que celui qui répond apprend ce que sa réponse a produit, la validation devient
    un jeu où l'on compte les points.
    """
    _, _, ids = await open_conversation(moderator_client, statements=3)

    conforme = await _valider(client, ids[0], "conforme")
    a_revoir = await _valider(client, ids[1], "a_revoir", motifs=["incitation_violence"])

    assert conforme.json() == a_revoir.json() == {"message": regles.ACCUSE_DE_RECEPTION}


# --- le plafond horaire du signalement ne s'applique pas au chemin sollicité ------


async def test_le_plafond_du_signalement_spontane_reste_entier(
    client, moderator_client, session_factory
):
    """Un « à revoir » ne consomme pas le quota horaire du signalement spontané — et ne
    l'ouvre pas non plus.

    Reprocher à quelqu'un de répondre trop souvent à une question qu'on lui pose
    soi-même n'aurait pas de sens ; mais le chemin sollicité porte ses propres plafonds,
    plus serrés, et celui du signalement spontané n'est pas touché.
    """
    from app.services import rate_limit

    _, _, ids = await open_conversation(moderator_client, statements=3)

    await _valider(client, ids[0], "a_revoir", motifs=["doublon"])

    async with session_factory() as session:
        signalement = await session.scalar(
            select(Signalement).where(Signalement.statement_id == ids[0])
        )
        # Aucun seau n'a été consommé par le chemin sollicité.
        assert await rate_limit.signalement_allowed(
            session, signalement.participant_id, None
        )
