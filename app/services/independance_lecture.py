"""Ce qui alimente les indicateurs d'indépendance depuis la base (MOD-5).

**Lecture seule, sans exception.** Ce module n'écrit rien, ne modifie rien, ne déclenche
rien. Il assemble ce que le MOD-3a stocke déjà — et **aucune donnée nouvelle n'est
collectée par ce lot**, ce qui était la condition du §7.

**Un seul signalement sur deux n'est pas un signalant** (MOD-4) : ceux que le site a
provoqués en sollicitant quelqu'un au hasard sont écartés ici, à la lecture. Voir la note
au corps de `observer` — c'est la garde qui empêche la détection de tempête de se
déclencher sur son propre sondage.

La règle et les seuils sont dans `app/services/independance.py`, qui ne connaît aucune
session : c'est ce qui permet de régler les indicateurs sur des scénarios écrits à la
main, sans base, et c'est la seule preuve dont ce lot dispose — la production n'a encore
reçu aucun signalement.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AnalysisRun,
    AnalysisStatus,
    Conversation,
    Participant,
    ParticipantProjection,
    Signalement,
    Statement,
    Vote,
)
from app.services.independance import Analyse, SignalantObserve, analyser


@dataclass(frozen=True, slots=True)
class DebatSurveille:
    """Un débat ayant reçu des signalements, et ce qu'on en lit."""

    conversation: Conversation
    analyse: Analyse
    signalements: int
    dernier: datetime | None


async def _groupes_du_debat(
    session: AsyncSession, conversation_id: int
) -> dict[int, int]:
    """participant_id -> `stable_group_id`, d'après le DERNIER calcul abouti.

    Un participant absent de ce dictionnaire **n'est dans aucun groupe**, et c'est une
    information, pas un trou : on n'est situé qu'en ayant assez voté. `independance.py`
    la traite comme telle.
    """
    run_id = await session.scalar(
        select(AnalysisRun.id)
        .where(
            AnalysisRun.conversation_id == conversation_id,
            AnalysisRun.status == AnalysisStatus.ok,
        )
        .order_by(AnalysisRun.finished_at.desc())
        .limit(1)
    )
    if run_id is None:
        return {}
    lignes = await session.execute(
        select(ParticipantProjection.participant_id, ParticipantProjection.stable_group_id)
        .where(
            ParticipantProjection.run_id == run_id,
            ParticipantProjection.stable_group_id.is_not(None),
        )
    )
    return {participant_id: groupe for participant_id, groupe in lignes}


def _heures_observees(conversation: Conversation, maintenant: datetime) -> float | None:
    """Depuis combien d'heures ce débat POUVAIT recevoir des signalements, dernière heure
    exclue.

    C'est le dénominateur du rythme habituel, et il compte **les heures creuses**. Une
    première version prenait la médiane des heures ACTIVES : une rafale de douze
    signalements en une heure y définissait sa propre référence à six par heure et
    ressortait à ×2, c'est-à-dire invisible. Le défaut n'était pas visible en test, qui
    fournissait le rythme au lieu de le faire calculer ; il est apparu au premier essai
    sur une vraie base.

    Le temps écoulé depuis l'ouverture du débat est le bon dénominateur : un débat qui
    reçoit un signalement par mois fait 0,001/h, et douze en une heure y est un
    événement.
    """
    ouverture = conversation.created_at
    if ouverture is None:  # pragma: no cover - colonne non nulle
        return None
    if ouverture.tzinfo is None:
        ouverture = ouverture.replace(tzinfo=timezone.utc)
    heures = (maintenant - ouverture).total_seconds() / 3600.0 - 1.0
    return heures if heures > 0 else None


async def observer(
    session: AsyncSession, conversation: Conversation, *, maintenant: datetime | None = None
) -> Analyse:
    """Les cinq indicateurs pour ce débat. **Aucune écriture.**"""
    maintenant = maintenant or datetime.now(timezone.utc)

    signalements = list(
        await session.scalars(
            select(Signalement)
            .join(Statement, Statement.id == Signalement.statement_id)
            .where(
                Statement.conversation_id == conversation.id,
                # **Les signalements SOLLICITÉS sont écartés, et c'est la garde la plus
                # importante de cette fonction** (MOD-4). Un « proposition à revoir »
                # rendu par quelqu'un que le site a tiré au sort dépose un signalement
                # ordinaire ; mais il n'est l'indice de rien, parce que c'est le site qui
                # a choisi qui le déposerait et sur quoi.
                #
                # Les laisser passer déclencherait la détection sur le sondage lui-même,
                # et pas par un effet de bord subtil : cinq validations sollicitées sur
                # une même proposition, dans la même heure, rendues par des identités
                # fraîches qui n'ont pas voté ce débat, c'est mot pour mot ce que
                # `rafale`, `fraicheur_des_identites` et `signalants_non_votants`
                # cherchent. Trois indicateurs sur cinq, là où deux suffisent à lever
                # une alerte.
                #
                # Le filtre est ICI et non dans `independance.py` : le module de règles
                # ne connaît aucune session et reçoit des `SignalantObserve`, qui ne
                # portent pas cette information. C'est à la lecture de décider ce qui
                # est un signalant.
                Signalement.sollicite.is_(False),
            )
            .order_by(Signalement.cree_le)
        )
    )
    if not signalements:
        return analyser([], heures_observees=None, maintenant=maintenant)

    groupes = await _groupes_du_debat(session, conversation.id)

    # Qui a voté dans CE débat : une requête pour tout le monde, pas une par signalant.
    votants = set(
        await session.scalars(
            select(Vote.participant_id)
            .join(Statement, Statement.id == Vote.statement_id)
            .where(Statement.conversation_id == conversation.id)
            .distinct()
        )
    )

    identifiants = {s.participant_id for s in signalements if s.participant_id}
    naissances: dict[int, datetime] = {}
    if identifiants:
        lignes = await session.execute(
            select(Participant.id, Participant.created_at).where(
                Participant.id.in_(identifiants)
            )
        )
        naissances = {pid: cree for pid, cree in lignes}

    observes = [
        SignalantObserve(
            participant_id=s.participant_id,
            groupe=groupes.get(s.participant_id) if s.participant_id else None,
            identite_creee_le=naissances.get(s.participant_id),
            a_vote_dans_le_debat=s.participant_id in votants,
            ip_hachee=s.ip_hachee,
            referent_hache=s.referent_hache,
            cree_le=s.cree_le,
        )
        for s in signalements
    ]
    return analyser(
        observes,
        heures_observees=_heures_observees(conversation, maintenant),
        maintenant=maintenant,
    )


async def debats_surveilles(
    session: AsyncSession, *, maintenant: datetime | None = None
) -> list[DebatSurveille]:
    """Les débats ayant reçu au moins un signalement, les plus alertants d'abord.

    Le tri met l'alerte en tête, puis le nombre d'indicateurs dépassés, puis le volume :
    un écran de surveillance se lit de haut en bas, et ce qui appelle une décision doit y
    être avant ce qui n'en appelle pas.
    """
    maintenant = maintenant or datetime.now(timezone.utc)
    lignes = await session.execute(
        select(
            Statement.conversation_id,
            func.count(Signalement.id),
            func.max(Signalement.cree_le),
        )
        .join(Statement, Statement.id == Signalement.statement_id)
        # Même garde qu'au corps d'`observer`, et il faut les deux (MOD-4). Sans
        # celle-ci, un débat dont TOUS les signalements sont sollicités entrerait dans
        # cet écran avec un compteur non nul et cinq indicateurs calculés sur zéro
        # signalant — « ce débat est surveillé » s'afficherait pour un débat dont
        # personne ne s'est plaint. Le décompte affiché compte donc les mêmes lignes que
        # celles qui alimentent les indicateurs, et pas d'autres.
        .where(Signalement.sollicite.is_(False))
        .group_by(Statement.conversation_id)
    )

    surveilles = []
    for conversation_id, combien, dernier in lignes:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:  # pragma: no cover - la cascade l'empêche
            continue
        surveilles.append(
            DebatSurveille(
                conversation=conversation,
                analyse=await observer(session, conversation, maintenant=maintenant),
                signalements=combien,
                dernier=dernier,
            )
        )

    return sorted(
        surveilles,
        key=lambda d: (
            not d.analyse.alerte,
            -len(d.analyse.depassements),
            -d.signalements,
        ),
    )
