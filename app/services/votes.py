"""Enregistrement des votes et choix de la proposition suivante."""

import math
import random

from sqlalchemy import func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AnalysisRun,
    AnalysisStatus,
    Conversation,
    ModerationStatus,
    Participant,
    Statement,
    StatementStat,
    Vote,
)


async def cast_vote(
    session: AsyncSession, participant: Participant, statement: Statement, value: int
) -> tuple[Vote, bool]:
    """Enregistre un vote, ou met à jour celui qui existe (re-vote).

    Un seul aller-retour, en `INSERT ... ON CONFLICT DO UPDATE` : deux requêtes
    concurrentes du même participant sur la même proposition ne peuvent pas créer
    deux lignes. Renvoie (vote, créé) — `xmax = 0` distingue une insertion d'une
    mise à jour, information qu'on ne peut pas déduire après coup.
    """
    statement_insert = (
        pg_insert(Vote)
        .values(
            participant_id=participant.id,
            statement_id=statement.id,
            value=value,
        )
        .on_conflict_do_update(
            constraint="uq_vote_participant_statement",
            set_={"value": value, "modified_at": func.now()},
        )
        # `xmax = 0` vaut vrai pour une ligne réellement insérée : c'est la seule
        # façon de distinguer insertion et mise à jour dans un ON CONFLICT.
        .returning(Vote.id, literal_column("(xmax = 0)").label("inserted"))
    )
    row = (await session.execute(statement_insert)).one()
    await session.commit()

    vote = await session.get(Vote, row.id)
    return vote, bool(row.inserted)


def _unvoted_statement_ids(conversation: Conversation, participant: Participant):
    """Identifiants des propositions approuvées que ce participant n'a pas vues.

    Seul l'identifiant, pas la ligne entière (chantier H) : le tirage n'a besoin que
    de l'id pour pondérer et choisir, et charger `Statement.text` (illimité en taille)
    pour chaque candidate nirait rien au tirage lui-même — mesuré à ~50 ms pour 500
    candidates, contre ~7 ms une fois la ligne complète remplacée par son seul id. Le
    gagnant, lui, est relu en entier après coup, une seule fois.
    """
    already_voted = select(Vote.statement_id).where(
        Vote.participant_id == participant.id
    )
    return (
        select(Statement.id)
        .where(
            Statement.conversation_id == conversation.id,
            Statement.moderation_status == ModerationStatus.approved,
            Statement.id.notin_(already_voted),
        )
    )


#: Poids d'une proposition dont aucun calcul n'a encore évalué la priorité, quand la
#: conversation n'a aucun score du tout. Sa valeur absolue est sans importance : tous
#: les poids valent alors la même chose, le tirage est uniforme.
DEFAULT_WEIGHT = 1.0

#: Plancher, en fraction du poids le plus élevé. Aucune proposition ne doit être
#: définitivement exclue du tirage : une priorité très basse doit ralentir sa
#: présentation, pas la supprimer — sans quoi elle ne récolterait jamais les votes qui
#: corrigeraient son estimation.
MIN_WEIGHT_RATIO = 0.01


def weight_for(priority: float | None, top_weight: float) -> float:
    """Poids de tirage d'une proposition.

    **Racine carrée de la priorité.** red-dwarf calcule
    `priorité = (importance × nouveauté)²` : en prendre la racine redonne exactement
    `importance × nouveauté`, la métrique AVANT son amplification finale. Ce n'est
    donc pas une atténuation arbitraire mais le score non amplifié — assez pour
    favoriser les propositions clivantes et neuves, pas assez pour qu'une poignée
    d'entre elles monopolise l'attention au détriment de la largeur de participation.

    Une proposition **non notée** (approuvée depuis le dernier calcul) reçoit le poids
    le plus élevé de la conversation : elle n'est pas peu prioritaire, elle est
    inconnue — et red-dwarf lui donnerait de toute façon un facteur de nouveauté ×9.
    """
    if priority is None:
        return top_weight
    return max(math.sqrt(priority), top_weight * MIN_WEIGHT_RATIO)


def draw_weights(
    statement_ids: list[int], priorities: dict[int, float | None]
) -> list[float]:
    """Poids de chaque candidate, dans l'ordre reçu."""
    notees = [
        math.sqrt(p)
        for sid in statement_ids
        if (p := priorities.get(sid)) is not None and p > 0
    ]
    top = max(notees) if notees else DEFAULT_WEIGHT
    poids = [weight_for(priorities.get(sid), top) for sid in statement_ids]
    if sum(poids) <= 0:  # pragma: no cover - filet
        return [1.0] * len(statement_ids)
    return poids


async def latest_priorities(
    session: AsyncSession, conversation: Conversation
) -> dict[int, float | None]:
    """Priorités du dernier calcul ABOUTI, lues à chaque appel.

    Aucune mise en cache et aucune file précalculée : c'est ce qui fait qu'une
    proposition devenue prioritaire remonte dès la proposition suivante servie à un
    participant déjà en train de voter.
    """
    run_id = await session.scalar(
        select(AnalysisRun.id)
        .where(
            AnalysisRun.conversation_id == conversation.id,
            AnalysisRun.status == AnalysisStatus.ok,
        )
        .order_by(AnalysisRun.finished_at.desc())
        .limit(1)
    )
    if run_id is None:
        return {}
    lignes = await session.execute(
        select(StatementStat.statement_id, StatementStat.priority).where(
            StatementStat.run_id == run_id, StatementStat.group_id.is_(None)
        )
    )
    return {statement_id: priority for statement_id, priority in lignes}


async def next_statement(
    session: AsyncSession,
    conversation: Conversation,
    participant: Participant,
    rng: random.Random | None = None,
) -> Statement | None:
    """Prochaine proposition à soumettre au participant.

    Tirage aléatoire **pondéré** parmi les propositions approuvées non encore vues.

    Pourquoi un tirage et non un ordre strict : les égalités de score sont la règle et
    non l'exception (des propositions aux mêmes compteurs ont exactement la même
    priorité). Un ordre strict les servirait dans l'ordre des identifiants, à tout le
    monde — les premières récolteraient tous les votes, les dernières aucun, et
    l'égalité ne se casserait jamais. Le tirage répartit la charge entre participants
    et laisse chaque proposition récolter de quoi affiner son propre score.
    """
    candidate_ids = list(
        await session.scalars(_unvoted_statement_ids(conversation, participant))
    )
    if not candidate_ids:
        return None

    priorities = await latest_priorities(session, conversation)
    poids = draw_weights(candidate_ids, priorities)
    chosen_id = (rng or random).choices(candidate_ids, weights=poids, k=1)[0]
    return await session.get(Statement, chosen_id)


async def remaining_count(
    session: AsyncSession, conversation: Conversation, participant: Participant
) -> int:
    subquery = _unvoted_statement_ids(conversation, participant).subquery()
    return await session.scalar(select(func.count()).select_from(subquery))


async def participants_de(session: AsyncSession, conversation: Conversation) -> int:
    """Personnes DISTINCTES ayant voté au moins une fois sur ce débat.

    Sert au bandeau de titre de la page d'un débat (chantier I2, instruction 9). Des
    personnes, pas des votes : quelqu'un qui répond aux huit propositions d'un débat
    compte pour un — même règle que `accueil.participants_de_la_semaine`, à laquelle ce
    chiffre est visuellement voisin sur le site.

    À NE PAS confondre avec `GroupView.total_participants`, qui compte les participants
    retenus par le DERNIER CALCUL de groupes : celui-là n'existe pas tant qu'aucun calcul
    n'a abouti, et il exclut ceux qui n'ont pas assez voté pour être situés. Le bandeau
    annonce « participants », pas « participants classés » — il lui faut le compte
    entier, disponible dès le premier vote.

    Seules les propositions APPROUVÉES comptent : un vote sur une proposition retirée
    par la modération ne fait pas de son auteur un participant de ce débat, et c'est le
    même filtrage que partout ailleurs.
    """
    return await session.scalar(
        select(func.count(func.distinct(Vote.participant_id)))
        .join(Statement, Statement.id == Vote.statement_id)
        .where(
            Statement.conversation_id == conversation.id,
            Statement.moderation_status == ModerationStatus.approved,
        )
    ) or 0


async def repartition_de(session: AsyncSession, statement_id: int) -> tuple[int, int, int]:
    """Accord, désaccord, passe sur UNE proposition — comptés dans `vote`, à l'instant.

    Sert au résultat immédiat de la carte de vote (chantier I2, instruction 10) : la
    personne vient de se prononcer, et voit aussitôt comment les autres l'ont fait sur
    la même proposition.

    **Pas `StatementStat`**, pour la raison que `resultats.py` développe en tête de
    fichier : cette table est écrite par le calcul, au mieux toutes les heures, et un
    résultat vieux d'une heure présenté à quelqu'un qui vient de voter serait faux —
    il pourrait même ne pas y compter son propre vote. Compter les votes est la seule
    réponse juste à « et les autres, qu'ont-ils répondu ? ».

    Le tri en trois valeurs suit celui du modèle : 1 accord, -1 désaccord, 0 passe.
    """
    lignes = (
        await session.execute(
            select(Vote.value, func.count(Vote.id))
            .where(Vote.statement_id == statement_id)
            .group_by(Vote.value)
        )
    ).all()
    comptes = {valeur: nombre for valeur, nombre in lignes}
    return (comptes.get(1, 0), comptes.get(-1, 0), comptes.get(0, 0))


async def parcours_de(
    session: AsyncSession, conversation: Conversation, participant: Participant
) -> tuple[int, int, int]:
    """Les votes de CETTE personne sur CE débat : accord, désaccord, passe.

    Sert au panneau « Votre parcours » (chantier I2, instruction 14). Le total est la
    somme des trois — c'est le même nombre que `groups.votes_emis`, compté ici en une
    requête au lieu de deux, et sur les mêmes lignes : tous les votes de la personne sur
    les propositions de ce débat.

    Aucune sous-catégorie inventée : ce sont les trois valeurs que `Vote.value` peut
    prendre (1, -1, 0), et rien d'autre n'est déduit.
    """
    lignes = (
        await session.execute(
            select(Vote.value, func.count(Vote.id))
            .join(Statement, Statement.id == Vote.statement_id)
            .where(
                Vote.participant_id == participant.id,
                Statement.conversation_id == conversation.id,
            )
            .group_by(Vote.value)
        )
    ).all()
    comptes = {valeur: nombre for valeur, nombre in lignes}
    return (comptes.get(1, 0), comptes.get(-1, 0), comptes.get(0, 0))


async def vote_count(session: AsyncSession, participant: Participant) -> int:
    return await session.scalar(
        select(func.count(Vote.id)).where(Vote.participant_id == participant.id)
    )
