"""Cycle de vie des participants : création anonyme, et rattachement à un compte."""

import secrets

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Participant, Statement, User

ANON_TOKEN_BYTES = 24


def new_anon_token() -> str:
    return secrets.token_urlsafe(ANON_TOKEN_BYTES)


async def by_anon_token(session: AsyncSession, token: str) -> Participant | None:
    return await session.scalar(
        select(Participant).where(Participant.anon_token == token)
    )


async def by_user(session: AsyncSession, user: User) -> Participant | None:
    return await session.scalar(
        select(Participant).where(Participant.user_id == user.id)
    )


async def create_anonymous(session: AsyncSession) -> Participant:
    participant = Participant(anon_token=new_anon_token())
    session.add(participant)
    await session.commit()
    await session.refresh(participant)
    return participant


async def create_for_user(session: AsyncSession, user: User) -> Participant:
    participant = Participant(user_id=user.id)
    session.add(participant)
    await session.commit()
    await session.refresh(participant)
    return participant


async def merge_into(
    session: AsyncSession, source: Participant, target: Participant
) -> dict[str, int]:
    """Verse l'activité d'un participant dans un autre, puis supprime le premier.

    **Le vote de la cible l'emporte** en cas de conflit : si la personne a voté sur
    la même proposition en anonyme puis sous son compte, c'est le vote du compte qui
    fait foi — c'est le plus récent et le plus intentionnel. Le `ON CONFLICT DO
    NOTHING` porte cette règle, sans branche conditionnelle.

    Les propositions déposées suivent aussi : elles restent l'œuvre de la même
    personne.

    **Les thèmes de prédilection aussi, et c'est moins évident.** Le chantier J range
    la préférence sur `participant` en s'appuyant sur le fait qu'une inscription garde
    la même ligne. C'est vrai du cas courant, mais pas d'ici : la fusion SUPPRIME la
    ligne source, et la préférence réglée en anonyme partirait avec elle. Elle est
    donc reprise, sous la même règle que les votes — **la cible l'emporte** : quelqu'un
    qui a déjà réglé ses thèmes sous son compte ne les voit pas remplacés par ceux
    d'un navigateur de passage.
    """
    moved_votes = await session.execute(
        text(
            """
            INSERT INTO vote (participant_id, statement_id, value, created_at, modified_at)
            SELECT :target, statement_id, value, created_at, modified_at
            FROM vote WHERE participant_id = :source
            ON CONFLICT ON CONSTRAINT uq_vote_participant_statement DO NOTHING
            """
        ),
        {"target": target.id, "source": source.id},
    )
    moved_statements = await session.execute(
        update(Statement)
        .where(Statement.author_participant_id == source.id)
        .values(author_participant_id=target.id)
    )
    # Les conversations PROPOSÉES suivent aussi. Sans cela, la clé étrangère en
    # ON DELETE SET NULL effacerait le lien avec la ligne du participant : la
    # conversation serait publiée sans appartenir à personne, et son auteur perdrait
    # points et badge. C'était le défaut relevé à la revue des situations limites.
    moved_conversations = await session.execute(
        update(Conversation)
        .where(Conversation.proposed_by_participant_id == source.id)
        .values(proposed_by_participant_id=target.id)
    )
    # Les thèmes ne sont repris que si la cible n'en a pas : NULL veut dire « jamais
    # réglé », donc il n'y a rien à écraser, alors qu'une liste vide serait un choix.
    themes_repris = 0
    if target.themes is None and source.themes:
        target.themes = list(source.themes)
        themes_repris = len(target.themes)

    # Les votes restants du participant source partent en cascade avec sa ligne.
    await session.delete(source)
    await session.commit()
    return {
        "votes": moved_votes.rowcount or 0,
        "statements": moved_statements.rowcount or 0,
        "conversations": moved_conversations.rowcount or 0,
        "themes": themes_repris,
    }


async def claim_for_user(
    session: AsyncSession, user: User, anon_token: str | None
) -> Participant:
    """Rattache l'activité anonyme courante au compte.

    Trois cas :
      1. Le compte n'a pas encore de participant et un participant anonyme
         correspond au jeton -> celui-ci devient le participant du compte. Rien à
         déplacer : c'est la même ligne, donc l'historique suit tout seul.
      2. Le compte a DÉJÀ un participant et un autre participant anonyme existe
         (connexion depuis un second navigateur) -> **fusion** : les votes, les
         propositions et les conversations proposées de l'anonyme sont versés dans
         celui du compte, le vote du compte l'emportant, puis la ligne anonyme est
         supprimée. Son jeton devient
         donc caduc, et la déconnexion en réémettra un neuf
         (cf. app/auth/deps.py).
      3. Ni l'un ni l'autre -> un participant neuf est créé pour le compte.
    """
    existing = await by_user(session, user)
    anonymous = await by_anon_token(session, anon_token) if anon_token else None
    if anonymous is not None and anonymous.user_id is not None:
        anonymous = None  # déjà rattaché à un compte : on n'y touche pas

    if existing is not None:
        if anonymous is not None and anonymous.id != existing.id:
            await merge_into(session, anonymous, existing)
        return existing

    if anonymous is not None:
        anonymous.user_id = user.id
        await session.commit()
        await session.refresh(anonymous)
        return anonymous

    return await create_for_user(session, user)
