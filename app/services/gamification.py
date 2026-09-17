"""Niveaux et badges.

Deux règles de comptage méritent d'être explicites, parce qu'elles décident du
comportement du système bien plus que la formule :

1. **Seules les conversations APPROUVÉES comptent.** Compter les propositions
   récompenserait le spam : il suffirait d'en soumettre en rafale pour monter de
   niveau, sans qu'aucune n'ait jamais été publiée.
2. **Tous les votes comptent**, y compris ceux émis en anonyme avant la création du
   compte : ils suivent le participant, qui suit le compte (cf. la fusion de C3).

La vérification d'e-mail n'entre nulle part dans ce calcul : un compte non vérifié
progresse normalement.
"""

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Badge,
    Conversation,
    ModerationStatus,
    Participant,
    User,
    UserBadge,
    Vote,
)
from app.models.gamification import BADGE_CATALOG, FIRST_CONVERSATION, FIRST_VOTE

#: 1 point par vote, 25 par conversation approuvée. Une conversation demande beaucoup
#: plus d'effort qu'un vote, sans devoir pour autant écraser la participation ordinaire.
POINTS_PER_VOTE = 1
POINTS_PER_CONVERSATION = 25

#: Table de paliers explicite plutôt qu'une formule : elle s'explique en une phrase à
#: un participant, et s'ajuste sans rien recalculer.
LEVELS: list[tuple[int, int, str]] = [
    (1, 0, "Nouvelle venue, nouveau venu"),
    (2, 10, "Participant"),
    (3, 30, "Habitué"),
    (4, 75, "Pilier"),
    (5, 150, "Référence"),
    (6, 300, "Vétéran"),
]


@dataclass
class Progress:
    votes: int
    conversations: int
    points: int
    level: int
    label: str
    #: None au dernier palier.
    next_level_at: int | None
    badges: list[Badge] = field(default_factory=list)

    @property
    def points_to_next(self) -> int | None:
        if self.next_level_at is None:
            return None
        return self.next_level_at - self.points

    @property
    def fraction(self) -> int:
        """Remplissage de l'anneau de niveau, en pourcentage entier (chantier F3).

        Mesuré **à l'intérieur du palier en cours**, et non depuis zéro. C'est la
        différence entre un anneau qui se referme au passage de niveau puis repart
        vide, et un anneau qui resterait presque plein pour toujours : à 30 points
        sur un palier suivant à 75, `30/75` afficherait 40 % alors que la personne
        vient tout juste d'entrer dans le palier et n'a rien parcouru.

        Au dernier palier, l'anneau est plein : il n'y a plus rien à parcourir, et un
        anneau vide y ferait croire à une progression perdue.
        """
        if self.next_level_at is None:
            return 100
        depart = next(seuil for numero, seuil, _ in LEVELS if numero == self.level)
        parcouru = self.points - depart
        total = self.next_level_at - depart
        if total <= 0:
            return 100
        return max(0, min(100, round(parcouru / total * 100)))


def points_for(votes: int, conversations: int) -> int:
    return votes * POINTS_PER_VOTE + conversations * POINTS_PER_CONVERSATION


def level_for(votes: int, conversations: int) -> tuple[int, str, int | None]:
    """Renvoie (niveau, intitulé, palier suivant en points)."""
    points = points_for(votes, conversations)
    current = LEVELS[0]
    for entry in LEVELS:
        if points >= entry[1]:
            current = entry
        else:
            return current[0], current[2], entry[1]
    return current[0], current[2], None


async def ensure_badges(session: AsyncSession) -> None:
    """Garantit la présence du catalogue.

    En production il est semé par la migration 0005 ; les tests, eux, créent les
    tables par `create_all` et n'exécutent pas les migrations — sans ce semis la clé
    étrangère `user_badge.badge_code` sauterait.
    """
    await session.execute(
        pg_insert(Badge).values(BADGE_CATALOG).on_conflict_do_nothing(
            index_elements=[Badge.code]
        )
    )
    await session.commit()


async def vote_count(session: AsyncSession, user: User) -> int:
    return await session.scalar(
        select(func.count(Vote.id))
        .join(Participant, Participant.id == Vote.participant_id)
        .where(Participant.user_id == user.id)
    )


async def conversation_count(session: AsyncSession, user: User) -> int:
    return await session.scalar(
        select(func.count(Conversation.id)).where(
            Conversation.owner_user_id == user.id,
            Conversation.moderation_status == ModerationStatus.approved,
        )
    )


async def progress_for(session: AsyncSession, user: User) -> Progress:
    votes = await vote_count(session, user)
    conversations = await conversation_count(session, user)
    level, label, next_at = level_for(votes, conversations)
    badges = list(
        await session.scalars(
            select(Badge)
            .join(UserBadge, UserBadge.badge_code == Badge.code)
            .where(UserBadge.user_id == user.id)
            .order_by(UserBadge.awarded_at)
        )
    )
    return Progress(
        votes=votes,
        conversations=conversations,
        points=points_for(votes, conversations),
        level=level,
        label=label,
        next_level_at=next_at,
        badges=badges,
    )


async def award(session: AsyncSession, user: User | None, code: str) -> bool:
    """Attribue un badge. Idempotent, et sans effet pour un visiteur sans compte.

    L'unicité est portée par la contrainte `uq_user_badge` : deux votes simultanés ne
    peuvent pas décerner deux fois « Premier vote ». Renvoie True si le badge vient
    d'être décerné.
    """
    if user is None:
        return False
    result = await session.execute(
        pg_insert(UserBadge)
        .values(user_id=user.id, badge_code=code)
        .on_conflict_do_nothing(constraint="uq_user_badge")
    )
    await session.commit()
    return bool(result.rowcount)


async def on_vote_cast(session: AsyncSession, participant: Participant) -> bool:
    """« Premier vote » — dès le premier vote émis sous un compte."""
    return await award(session, participant.user, FIRST_VOTE)


async def on_conversation_approved(
    session: AsyncSession, conversation: Conversation
) -> bool:
    """« Première conversation créée » — au moment de l'APPROBATION, pas de la
    proposition : décerner un badge pour une conversation qui sera ensuite rejetée
    serait incohérent avec le comptage des niveaux."""
    if conversation.owner_user_id is None:
        return False
    owner = await session.get(User, conversation.owner_user_id)
    return await award(session, owner, FIRST_CONVERSATION)
