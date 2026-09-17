"""Vote.

Passe par `get_current_participant`, comme les propositions de C2 : un anonyme et un
compte non vérifié votent aussi bien qu'un compte confirmé. C'était le contrat annoncé
depuis C1 ; il est ici tenu littéralement, sans dépendance particulière au vote.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_participant
from app.db import get_session
from app.models.gamification import FIRST_VOTE
from app.models import (
    ConversationState,
    ModerationStatus,
    Participant,
    Statement,
    VoteValue,
)
from app.models import Badge
from app.schemas import (
    BadgeRead,
    GroupeAnnonce,
    NextStatement,
    VoteCreate,
    Parcours,
    ResultatImmediat,
    ValidationProposee,
    VoteRecorded,
    statement_read,
)
from app.services import conversations as conversations_service
from app.services import recalcul
from app.services import resultats
from app.services import groups as groups_service
from app.services import gamification
from app.services import validation as regles_validation
from app.services import validation_tirage
from app.services import votes as votes_service
from app.services.liens_verification import signalements
from app.services.reformulation import acceptees_par_proposition

router = APIRouter(prefix="/api/conversations", tags=["votes"])


async def _open_conversation(session: AsyncSession, slug: str):
    conversation = await conversations_service.by_slug(session, slug)
    if conversation is None or not conversations_service.is_published(conversation):
        raise HTTPException(status_code=404, detail="Conversation inconnue")
    if conversation.state is not ConversationState.open:
        raise HTTPException(status_code=409, detail="Cette conversation est close.")
    return conversation


@router.get("/{slug}/next-statement", response_model=NextStatement)
async def next_statement(
    slug: str,
    session: AsyncSession = Depends(get_session),
    participant: Participant = Depends(get_current_participant),
):
    conversation = await _open_conversation(session, slug)
    statement = await votes_service.next_statement(session, conversation, participant)
    remaining = await votes_service.remaining_count(session, conversation, participant)
    # Les liens morts de la proposition servie (K3). Une requête, et aucune quand la
    # proposition ne porte pas de lien — c'est le cas de la plupart d'entre elles.
    signales = (
        await signalements(session, [source.url for source in statement.sources])
        if statement is not None
        else {}
    )
    # La reformulation acceptée de la proposition servie (MOD-17) : une requête, et
    # aucune quand il n'y a pas de proposition à servir.
    reformulees = (
        await acceptees_par_proposition(session, [statement.id])
        if statement is not None
        else {}
    )
    return NextStatement(
        participant_id=participant.id,
        remaining=remaining,
        statement=(
            statement_read(statement, signales, reformulees)
            if statement is not None
            else None
        ),
        # Le groupe n'est lu qu'au dernier appel, celui qui n'a plus de proposition à
        # servir : c'est le seul moment où on l'annonce, et le faire à chaque vote
        # ajouterait quatre requêtes par proposition pour un chiffre qui ne change
        # qu'au recalcul suivant.
        groupe=(await _annonce_de_groupe(session, conversation, participant))
        if statement is None
        else None,
    )


async def _annonce_de_groupe(
    session: AsyncSession, conversation, participant: Participant
) -> GroupeAnnonce:
    """Ce qu'on dit à quelqu'un qui vient de finir de voter.

    **Ce qu'on peut annoncer, et ce qu'on ne peut pas.** Le groupe vient du dernier
    calcul ABOUTI, comme partout ailleurs sur le site. Quelqu'un que ce calcul situe
    déjà s'entend nommer son groupe tout de suite. Quelqu'un qui vient de voter pour la
    première fois, lui, n'est dans aucun calcul : sa place n'existe pas encore.

    On ne la devine pas. La base de projection (moyenne et composantes principales) est
    calculée à chaque analyse puis **jetée** — rien ne la persiste —, si bien que placer
    quelqu'un en direct demanderait de la recalculer, sur une route publique en lecture.
    Et l'approcher autrement (par ressemblance aux votes moyens de chaque groupe) rendrait
    un groupe qui pourrait contredire la carte une heure plus tard. Annoncer « groupe B »
    puis afficher un point dans le groupe A serait pire que d'attendre.

    Le message de repli n'est donc pas un silence : `groups.for_participant` dit soit
    combien de votes il manque, soit que le prochain calcul situera la personne.
    """
    vue = await groups_service.for_participant(session, conversation, participant)
    if vue is None:
        # Aucun calcul abouti : il n'y a pas encore de groupes du tout, et parler de
        # « prochain calcul » supposerait qu'il en existe déjà un qui a marché.
        return GroupeAnnonce(
            raison=(
                "Les groupes d'opinion n'ont pas encore pu être calculés : "
                "cette consultation est trop jeune."
            )
        )
    return GroupeAnnonce(
        nom=vue.name,
        autres=vue.others,
        raison=vue.reason,
        prochain_calcul=_prochain_calcul_utile(vue),
    )


def _prochain_calcul_utile(vue) -> datetime | None:
    """L'instant du prochain calcul, quand l'annoncer sert à quelqu'un (chantier G9).

    Trois situations, et une seule appelle un compte à rebours :

    - **la personne est située** — elle sait déjà, et lui compter les minutes qui la
      séparent d'un chiffre qu'elle a sous les yeux serait du bruit ;
    - **il lui manque des votes** — attendre ne la situera pas. C'est voter qui la
      débloque, et un délai affiché à côté de « il vous faut 6 votes » laisserait croire
      le contraire ;
    - **elle a assez voté mais le dernier calcul est antérieur à ses votes** — c'est le
      cas visé : elle a fait tout ce qu'on lui demande, il ne lui reste qu'à attendre,
      et c'est cette attente-là qu'on chiffre.

    Le partage se lit sur `vue` sans requête de plus : `groups.for_participant` l'a déjà
    tranché pour écrire son motif, et le porte dans `attend_le_calcul`.
    """
    if vue.name is not None or not vue.attend_le_calcul:
        return None
    return recalcul.prochain_calcul()


async def _carte_de_validation(
    session: AsyncSession,
    conversation,
    participant: Participant,
    *,
    votes_emis: int,
    vient_de_voter: int,
) -> ValidationProposee | None:
    """La carte de validation, quand ce vote-ci en déclenche une (MOD-4).

    **Deux conditions, dans cet ordre, et l'ordre fait tout le coût.** La cadence
    d'abord — une pure division, sans base : six votes sur sept sortent d'ici sans
    qu'aucune requête n'ait été faite. Le tirage ensuite, et lui seul touche la base.
    Inverser reviendrait à interroger la base à chaque vote du site pour jeter le
    résultat six fois sur sept.

    **`None` ne se distingue pas d'un `None`**, et c'est voulu : plafond du jour atteint,
    débat dont tout a déjà été regardé, ou simplement pas le bon tour — le navigateur
    reçoit la même absence, et n'apprend donc rien sur l'état du dossier.
    """
    if not regles_validation.sollicitation_due(votes_emis):
        return None
    proposition = await validation_tirage.a_proposer(
        session, conversation, participant, sauf_id=vient_de_voter
    )
    if proposition is None:
        return None
    return ValidationProposee(
        statement_id=proposition.id,
        texte=proposition.text,
        titre=regles_validation.TITRE,
        consigne=regles_validation.CONSIGNE,
        libelle_conforme=regles_validation.LIBELLES[regles_validation.Verdict.conforme],
        libelle_a_revoir=regles_validation.LIBELLES[regles_validation.Verdict.a_revoir],
    )


@router.post("/{slug}/votes", response_model=VoteRecorded, status_code=200)
async def cast_vote(
    slug: str,
    payload: VoteCreate,
    session: AsyncSession = Depends(get_session),
    participant: Participant = Depends(get_current_participant),
):
    conversation = await _open_conversation(session, slug)

    if payload.value not in VoteValue.values():
        raise HTTPException(
            status_code=422,
            detail="Valeur de vote invalide : attendu +1 (d'accord), -1 (pas d'accord) ou 0 (passer).",
        )

    statement = await session.get(Statement, payload.statement_id)
    if (
        statement is None
        or statement.conversation_id != conversation.id
        or statement.moderation_status is not ModerationStatus.approved
    ):
        # Même réponse pour « inconnue » et « non approuvée » : sinon on révélerait
        # l'existence de propositions en attente de modération.
        raise HTTPException(status_code=404, detail="Proposition inconnue")

    vote, created = await votes_service.cast_vote(
        session, participant, statement, payload.value
    )
    # Sans effet pour un visiteur sans compte, et idempotent : la contrainte
    # uq_user_badge porte la règle « une seule fois ».
    awarded = await gamification.on_vote_cast(session, participant)
    badge = None
    if awarded:
        row = await session.get(Badge, FIRST_VOTE)
        if row is not None:
            badge = BadgeRead(
                code=row.code, label=row.label, description=row.description, icon=row.icon
            )
    # Le résultat immédiat (chantier I2, instruction 10) : ce que les autres ont
    # répondu sur la proposition qu'on vient de trancher, ce vote-ci compris. Les
    # comptes sont LUS DANS `vote`, pas dans `statement_stat` — voir la note de
    # `votes_service.repartition_de`. Les pourcentages passent par
    # `resultats.pourcentages`, la même fonction que la page de résultats : deux
    # arrondis écrits séparément finiraient par se contredire sur un cas limite.
    n_accord, n_desaccord, n_passe = await votes_service.repartition_de(
        session, statement.id
    )
    total = n_accord + n_desaccord + n_passe
    part_accord, part_desaccord, part_passe = resultats.pourcentages(
        n_accord, n_desaccord, total
    )
    # Et de quoi tenir la barre de progression sans recharger la page. Le seuil est
    # celui de CE débat — variable, borné par le nombre de propositions —, jamais un
    # nombre fixe.
    seuil_situe = await groups_service.min_user_votes_for(session, conversation)
    # Le parcours donne le total ET sa répartition en une requête ; `votes_emis` en
    # aurait fait une seconde pour le même nombre.
    p_accord, p_desaccord, p_passe = await votes_service.parcours_de(
        session, conversation, participant
    )
    emis = p_accord + p_desaccord + p_passe
    return VoteRecorded(
        badge_awarded=FIRST_VOTE if awarded else None,
        badge=badge,
        participant_id=participant.id,
        statement_id=statement.id,
        value=vote.value,
        created=created,
        resultat=ResultatImmediat(
            n_accord=n_accord,
            n_desaccord=n_desaccord,
            n_passe=n_passe,
            total=total,
            part_accord=part_accord,
            part_desaccord=part_desaccord,
            part_passe=part_passe,
        ),
        seuil_situe=seuil_situe,
        votes_emis=emis,
        parcours=Parcours(
            accord=p_accord, desaccord=p_desaccord, passe=p_passe, total=emis
        ),
        # La carte de validation, une fois toutes les sept propositions votées (MOD-4).
        # Elle est calculée APRÈS le vote, sur le compte qui inclut celui-ci : c'est le
        # même nombre que celui de la barre de progression, et deux façons de compter
        # finiraient par se contredire.
        validation=await _carte_de_validation(
            session,
            conversation,
            participant,
            votes_emis=emis,
            vient_de_voter=statement.id,
        ),
        remaining=await votes_service.remaining_count(
            session, conversation, participant
        ),
    )
