"""Lecture des groupes d'opinion, côté participant.

Ne lit que le dernier calcul **abouti** : un recalcul en cours ou en échec ne doit
jamais changer ce qu'on affiche. Et n'expose que l'identité *stable* — l'étiquette
brute de k-means change à chaque recalcul et n'a aucun sens pour un lecteur.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.matching import group_name
from app.analysis.thresholds import min_user_votes_for
from app.services import recalcul
from app.models import (
    AnalysisRun,
    AnalysisStatus,
    Conversation,
    Participant,
    ParticipantProjection,
    Statement,
    Vote,
)


#: En dessous, on n'annonce pas de groupe : dire « vous êtes dans le groupe C, avec
#: 0 autre personne » désignerait publiquement un participant isolé, et n'apprend rien
#: à l'intéressé. Un groupe d'une seule personne est un artefact du découpage, pas une
#: opinion partagée.
MIN_GROUP_SIZE = 2


@dataclass
class GroupView:
    """Ce qu'on montre à une personne. `name` vaut None quand elle n'est pas classée."""

    name: str | None
    #: Nombre d'AUTRES personnes du même groupe (la personne elle-même exclue).
    others: int
    total_participants: int
    group_count: int
    computed_at: datetime | None
    #: Explication quand `name` est None — jamais un silence.
    reason: str | None = None
    #: Vrai quand la personne a fait sa part et qu'il ne lui reste qu'à attendre le
    #: prochain calcul. Faux quand il lui manque des votes : attendre ne la situerait
    #: pas. C'est ce partage, et non le texte du motif, qui décide si on lui chiffre
    #: son attente (chantier G9) — lire une prose pour en déduire un état, c'est perdre
    #: l'état à la première reformulation.
    attend_le_calcul: bool = False
    #: Seuil de votes personnels requis pour être situé, et votes déjà émis — remplis
    #: UNIQUEMENT dans le cas « il manque des votes », jamais dans les deux autres
    #: (attente du calcul, groupe trop petit). Le gabarit (chantier I) s'en sert pour
    #: habiller la carte encore verrouillée avec les vrais chiffres plutôt que de
    #: reformuler `reason` en JavaScript, ce qui l'aurait fait diverger du texte.
    seuil_votes: int | None = None
    votes_emis: int | None = None


async def latest_ok_run(
    session: AsyncSession, conversation: Conversation
) -> AnalysisRun | None:
    return await session.scalar(
        select(AnalysisRun)
        .where(
            AnalysisRun.conversation_id == conversation.id,
            AnalysisRun.status == AnalysisStatus.ok,
        )
        .order_by(AnalysisRun.finished_at.desc())
        .limit(1)
    )


async def votes_emis(
    session: AsyncSession, conversation: Conversation, participant: Participant
) -> int:
    """Combien de votes cette personne a émis sur ce débat.

    Public depuis le chantier I2 (instruction 10) : la barre de progression de la carte
    de vote annonce « encore N votes pour être situé », et il lui faut ce compte-ci —
    le même que celui dont `for_participant` se sert pour écrire son motif. Deux
    comptages séparés auraient fini par afficher deux chiffres différents sur le même
    écran.
    """
    return await session.scalar(
        select(func.count(Vote.id))
        .join(Statement, Statement.id == Vote.statement_id)
        .where(
            Vote.participant_id == participant.id,
            Statement.conversation_id == conversation.id,
        )
    )


async def for_participant(
    session: AsyncSession, conversation: Conversation, participant: Participant
) -> GroupView | None:
    """Groupe de cette personne dans le dernier calcul abouti.

    Renvoie None si aucun calcul n'a encore abouti — il n'y a alors rien à dire.
    """
    run = await latest_ok_run(session, conversation)
    if run is None:
        return None

    total = await session.scalar(
        select(func.count(ParticipantProjection.id)).where(
            ParticipantProjection.run_id == run.id
        )
    )
    group_count = await session.scalar(
        select(func.count(func.distinct(ParticipantProjection.stable_group_id))).where(
            ParticipantProjection.run_id == run.id,
            ParticipantProjection.stable_group_id.isnot(None),
        )
    )

    stable = await session.scalar(
        select(ParticipantProjection.stable_group_id).where(
            ParticipantProjection.run_id == run.id,
            ParticipantProjection.participant_id == participant.id,
        )
    )

    if stable is None:
        # Le seuil annoncé est celui qui s'applique VRAIMENT à cette conversation
        # (borné par le nombre de propositions) : annoncer 7 votes sur une
        # conversation qui n'en compte que 3 serait une consigne intenable.
        seuil = await min_user_votes_for(session, conversation)
        emis = await votes_emis(session, conversation, participant)
        if emis < seuil:
            raison = (
                f"Il faut {seuil} votes pour être situé ; vous en avez émis {emis}."
            )
        else:
            # La cadence est LUE, jamais écrite ici : « toutes les heures » a survécu
            # en dur à ce paragraphe jusqu'au G9, alors que le G2 avait déjà rendu la
            # fréquence réglable partout ailleurs. Le temps restant, lui, n'est pas
            # dans ce motif : il est calculé à l'instant où le message s'affiche
            # (voir `GroupeAnnonce.prochain_calcul`), sans quoi une page ouverte
            # depuis dix minutes annoncerait un délai périmé.
            raison = (
                "Vos votes seront pris en compte au prochain calcul, "
                f"qui a lieu {recalcul.periode_de_recalcul()}."
            )
        return GroupView(
            name=None,
            others=0,
            total_participants=total,
            group_count=group_count,
            computed_at=run.finished_at,
            reason=raison,
            attend_le_calcul=emis >= seuil,
            seuil_votes=None if emis >= seuil else seuil,
            votes_emis=None if emis >= seuil else emis,
        )

    members = await session.scalar(
        select(func.count(ParticipantProjection.id)).where(
            ParticipantProjection.run_id == run.id,
            ParticipantProjection.stable_group_id == stable,
        )
    )

    if members < MIN_GROUP_SIZE:
        return GroupView(
            name=None,
            others=0,
            total_participants=total,
            group_count=group_count,
            computed_at=run.finished_at,
            reason=(
                "Vos réponses ne vous rattachent pour l'instant à aucun groupe "
                "constitué. C'est fréquent quand une consultation démarre."
            ),
        )

    return GroupView(
        name=group_name(stable),
        others=members - 1,
        total_participants=total,
        group_count=group_count,
        computed_at=run.finished_at,
    )
