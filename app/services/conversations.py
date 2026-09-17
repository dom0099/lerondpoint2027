"""Conversations, propositions et modération."""

import re
import secrets
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Conversation,
    ConversationState,
    ConversationTheme,
    ModerationMode,
    ModerationStatus,
    Participant,
    Statement,
    StatementSource,
    User,
)
from app.services.liens import Lien
from app.services.themes import themes_connus
from app.services import video as video_service

MAX_STATEMENT_LENGTH = 500
#: Borne de la colonne `conversation.title`.
MAX_TITLE_LENGTH = 300

#: Une conversation proposée sans proposition d'amorce serait ouverte sur le vide :
#: le premier visiteur n'aurait rien à voter, et personne ne proposerait de proposition
#: dans une conversation que personne ne visite. Trois amorces donnent aussi au
#: modérateur de quoi juger l'intention, ce qu'un titre seul ne montre pas.
MIN_SEED_STATEMENTS = 3
MAX_SEED_STATEMENTS = 10


@dataclass(frozen=True)
class Amorce:
    """Une proposition d'amorce et ses liens.

    Existe pour que `propose_conversation` n'ait pas à recevoir deux listes alignées
    par indice — un alignement qu'aucune signature ne garantit et que le premier
    filtrage de propositions vides casserait en silence.
    """

    text: str
    liens: tuple[Lien, ...] = ()


def poser_themes(conversation: Conversation, codes: Sequence[str]) -> list[str]:
    """Remplace l'étiquetage d'un débat, et rend les codes retenus.

    **Permissif à dessein** : les codes inconnus sont écartés, et zéro thème est
    accepté. C'est le pendant de `themes_connus` — la règle « 1 à 3 » n'appartient pas
    à cette fonction mais à l'écran qui écrit, parce qu'elle ne s'applique pas partout :
    un brouillon peut n'en porter aucun, une publication non.

    **Modifie ce qui diffère, et rien d'autre.** Réaffecter la collection en bloc
    paraissait plus simple, et c'est un piège : dans un même vidage de session,
    SQLAlchemy peut émettre l'INSERT de la ligne neuve AVANT le DELETE de l'ancienne,
    et `uq_conversation_theme` refuse alors le doublon. Le symptôme n'apparaît qu'au
    SECOND enregistrement d'un même débat avec le même thème — c'est-à-dire jamais dans
    un test qui n'enregistre qu'une fois, et systématiquement dès qu'un modérateur
    ouvre puis clôt une conversation.

    Le calcul par différence évite le problème au lieu de le contourner, et il a un
    mérite de plus : ré-enregistrer un débat sans toucher à ses thèmes n'écrit rien.
    """
    codes_retenus = themes_connus(codes)
    voulus = set(codes_retenus)
    existants = {lien.code: lien for lien in conversation.themes}

    for code, lien in existants.items():
        if code not in voulus:
            conversation.themes.remove(lien)  # `delete-orphan` supprime la ligne
    for code in codes_retenus:
        if code not in existants:
            conversation.themes.append(ConversationTheme(code=code))
    return codes_retenus


def _attacher_liens(statement: Statement, liens: Sequence[Lien]) -> None:
    """Pose les liens sur une proposition, numérotés à partir de 1.

    La position vient de l'ordre de saisie et non de l'identifiant : c'est ce qui
    permettra d'échanger deux liens sans recréer les lignes. Le plafond n'est pas
    revérifié ici — `app/services/liens.py` l'a fait sur la saisie, et la contrainte
    `position in (1, 2)` le tient de toute façon en base.
    """
    statement.sources = [
        StatementSource(position=rang, url=lien.url, label=lien.libelle)
        for rang, lien in enumerate(liens, start=1)
    ]


def slugify(title: str) -> str:
    """Transforme un titre en fragment d'URL lisible."""
    normalized = unicodedata.normalize("NFKD", title)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")[:48]
    return slug or "conversation"


async def unique_slug(session: AsyncSession, title: str) -> str:
    """Slug lisible, suffixé seulement en cas de collision.

    Attention : lire puis écrire n'est pas atomique. Deux requêtes simultanées avec
    le même titre calculent le même slug et la seconde viole l'index unique — d'où
    le rattrapage dans `_insert_conversation`.
    """
    base = slugify(title)
    candidate = base
    while await session.scalar(
        select(Conversation.id).where(Conversation.slug == candidate)
    ):
        candidate = f"{base}-{secrets.token_hex(3)}"
    return candidate


async def _insert_conversation(session: AsyncSession, build, title: str) -> Conversation:
    """Insère une conversation en rattrapant la course sur le slug.

    Deux propositions simultanées du même titre renvoyaient une erreur 500 : la
    seconde violait l'index unique. On réessaie avec un suffixe aléatoire plutôt que
    de faire échouer la demande.
    """
    for tentative in range(4):
        conversation = build(await unique_slug(session, title))
        try:
            # Point de sauvegarde plutôt que `rollback()` : un rollback complet
            # expirerait TOUS les objets de la session — dont le participant, dont
            # l'accès déclencherait ensuite une lecture hors contexte asynchrone
            # (MissingGreenlet). Le savepoint n'annule que l'insertion ratée.
            async with session.begin_nested():
                session.add(conversation)
        except IntegrityError:
            if tentative == 3:
                raise
            continue
        await session.commit()
        await session.refresh(conversation)
        return conversation
    raise RuntimeError("slug introuvable")  # pragma: no cover


async def create_conversation(
    session: AsyncSession,
    *,
    owner: User,
    title: str,
    description: str | None = None,
    moderation_mode: ModerationMode = ModerationMode.pre,
) -> Conversation:
    """Création par un modérateur : approuvée d'emblée, il EST la modération."""
    return await _insert_conversation(
        session,
        lambda slug: Conversation(
            slug=slug,
            title=title,
            description=description,
            owner_user_id=owner.id,
            moderation_mode=moderation_mode,
            moderation_status=ModerationStatus.approved,
        ),
        title,
    )


async def propose_conversation(
    session: AsyncSession,
    *,
    participant: Participant,
    title: str,
    description: str | None,
    statements: Sequence[str | Amorce],
    themes: Sequence[str] = (),
    video: video_service.ResultatTranscodage | None = None,
) -> Conversation:
    """Proposition par un participant : la conversation ET ses amorces sont en attente.

    Les amorces sont créées `pending` et n'apparaissent PAS dans la file des
    propositions : elles se jugent avec la conversation, en un seul geste. Le modérateur
    garde la main sur chacune ensuite, par l'éditeur de conversation.

    `video`, s'il est fourni, est DÉJÀ transcodé — c'est à l'appelant (le routeur) de
    l'avoir fait AVANT cet appel, pour pouvoir réafficher le formulaire en cas d'échec
    sans avoir créé de conversation à moitié pourvue (chantier Vidéo, VIDEO-3). La
    miniature est celle choisie automatiquement par `preparer()` : le formulaire
    participant n'offre pas l'écran à trois candidates du modérateur.
    """
    conversation = await _insert_conversation(
        session,
        lambda slug: Conversation(
            slug=slug,
            title=title,
            description=description,
            # Renseigné à l'approbation si l'auteur a (ou obtient) un compte.
            owner_user_id=participant.user_id,
            proposed_by_participant_id=participant.id,
            moderation_status=ModerationStatus.pending,
            state=ConversationState.draft,
            video_chemin=video.chemin_video if video else None,
            video_miniature_chemin=video.chemin_miniature if video else None,
            video_duree_secondes=video.duree_s if video else None,
            video_profil=video.profil if video else None,
        ),
        title,
    )

    # Les thèmes du proposeur sont une SUGGESTION : le modérateur les voit et les
    # corrige à l'approbation, dans la file. C'est le même geste que l'approbation
    # elle-même, et non une décision de plus.
    poser_themes(conversation, themes)

    for entree in statements:
        # `str` accepté autant qu'`Amorce` : la grande majorité des appels (et tous
        # les tests antérieurs au chantier J) ne connaissent que le texte.
        amorce = entree if isinstance(entree, Amorce) else Amorce(entree)
        statement = Statement(
            conversation_id=conversation.id,
            text=amorce.text.strip(),
            author_participant_id=participant.id,
            is_seed=True,
            moderation_status=ModerationStatus.pending,
        )
        _attacher_liens(statement, amorce.liens)
        session.add(statement)
    await session.commit()
    await session.refresh(conversation)
    return conversation


async def pending_conversations(session: AsyncSession) -> list[Conversation]:
    result = await session.scalars(
        select(Conversation)
        .where(Conversation.moderation_status == ModerationStatus.pending)
        .order_by(Conversation.created_at)
    )
    return list(result)


async def moderate_conversation(
    session: AsyncSession, conversation: Conversation, moderator: User, *, approve: bool
) -> Conversation:
    """Approuve ou rejette une conversation ET ses amorces, en une décision."""
    now = datetime.now(timezone.utc)
    status = ModerationStatus.approved if approve else ModerationStatus.rejected

    conversation.moderation_status = status
    if approve:
        # Approuver signifie publier : sans cela le modérateur devrait encore
        # l'ouvrir à la main, et une conversation approuvée resterait invisible.
        conversation.state = ConversationState.open
        # L'auteur a pu créer son compte APRÈS avoir proposé : on rattache alors.
        if conversation.owner_user_id is None and conversation.proposed_by_participant_id:
            author = await session.get(
                Participant, conversation.proposed_by_participant_id
            )
            if author is not None and author.user_id is not None:
                conversation.owner_user_id = author.user_id

    await session.execute(
        update(Statement)
        .where(
            Statement.conversation_id == conversation.id,
            Statement.is_seed.is_(True),
            Statement.moderation_status == ModerationStatus.pending,
        )
        .values(
            moderation_status=status,
            moderated_by_user_id=moderator.id,
            moderated_at=now,
        )
    )
    await session.commit()
    await session.refresh(conversation)
    return conversation


async def by_slug(session: AsyncSession, slug: str) -> Conversation | None:
    return await session.scalar(select(Conversation).where(Conversation.slug == slug))


async def by_id(session: AsyncSession, conversation_id: int) -> Conversation | None:
    return await session.get(Conversation, conversation_id)


async def list_conversations(session: AsyncSession) -> list[Conversation]:
    result = await session.scalars(
        select(Conversation).order_by(Conversation.created_at.desc())
    )
    return list(result)


class DuplicateStatement(Exception):
    """Ce texte existe déjà, mot pour mot, dans cette conversation."""


async def add_seed_statement(
    session: AsyncSession,
    conversation: Conversation,
    text: str,
    *,
    is_meta: bool = False,
    liens: Sequence[Lien] = (),
) -> Statement:
    """Proposition d'amorce écrite par le modérateur : approuvée d'emblée.

    Elle peut porter des liens comme n'importe quelle autre proposition (question 3) :
    c'est souvent là qu'un chiffre a le plus de valeur, puisque les amorces donnent le
    ton du débat et sont les premières votées.
    """
    text = text.strip()
    if len(text) > MAX_STATEMENT_LENGTH:
        # Le chemin modérateur n'avait aucune limite : une amorce de 5000 caractères
        # passait, alors que le chemin participant refuse au-delà de 500.
        raise ValueError(
            f"Proposition trop longue (max {MAX_STATEMENT_LENGTH} caractères)."
        )
    statement = Statement(
        conversation_id=conversation.id,
        text=text,
        is_seed=True,
        is_meta=is_meta,
        moderation_status=ModerationStatus.approved,
        moderated_at=datetime.now(timezone.utc),
    )
    _attacher_liens(statement, liens)
    try:
        # Savepoint : voir la note de `_insert_conversation`. Un rollback complet
        # expirerait les autres objets de la session.
        async with session.begin_nested():
            session.add(statement)
    except IntegrityError as exc:
        raise DuplicateStatement from exc
    await session.commit()
    await session.refresh(statement)
    return statement


async def submit_statement(
    session: AsyncSession,
    conversation: Conversation,
    participant: Participant,
    text: str,
    liens: Sequence[Lien] = (),
) -> Statement:
    """Proposition d'un participant. **Publiée d'emblée, sans exception** (MOD-14).

    Il n'y a plus de relecture avant publication : une proposition déposée est visible
    tout de suite, et le contrôle se fait APRÈS, par le signalement — n'importe qui peut
    signaler, un motif de ligne rouge retire aussitôt (`signalement_file`), un motif de
    forme route vers reformulation ou fusion (`signalement.router`).

    **`conversation.moderation_mode` n'est plus consulté ici**, et c'est un choix, pas un
    oubli : la colonne survit au lot (la retirer coûterait une recréation de type
    PostgreSQL pour rien), mais plus aucun écran ne la règle et plus aucun code ne la lit
    pour décider du sort d'une proposition. Une conversation restée en base à `pre` ne
    retient donc plus rien — c'est exactement l'effet voulu, et il vaut mieux qu'il soit
    écrit ici qu'à déduire d'une colonne muette.
    """
    statement = Statement(
        conversation_id=conversation.id,
        text=text.strip(),
        author_participant_id=participant.id,
        is_seed=False,
        moderation_status=ModerationStatus.approved,
    )
    # Les liens sont posés AVANT l'insertion : ils voyagent avec la proposition, dans
    # la même transaction. Une proposition enregistrée dont les liens échoueraient
    # ensuite serait publiée amputée de ce qui l'appuie, sans que personne le sache.
    _attacher_liens(statement, liens)
    try:
        # L'unicité (conversation_id, text) porte la règle : un double clic ne crée
        # plus six fois la même proposition. Savepoint plutôt que rollback complet.
        async with session.begin_nested():
            session.add(statement)
    except IntegrityError as exc:
        raise DuplicateStatement from exc
    await session.commit()
    await session.refresh(statement)
    return statement


async def visible_statements(
    session: AsyncSession, conversation: Conversation
) -> list[Statement]:
    """Ce que voit un participant : uniquement les propositions approuvées."""
    result = await session.scalars(
        select(Statement)
        .where(
            Statement.conversation_id == conversation.id,
            Statement.moderation_status == ModerationStatus.approved,
        )
        .order_by(Statement.id)
    )
    return list(result)


async def all_statements(
    session: AsyncSession, conversation: Conversation
) -> list[Statement]:
    result = await session.scalars(
        select(Statement)
        .where(Statement.conversation_id == conversation.id)
        .order_by(Statement.id)
    )
    return list(result)


async def pending_count(session: AsyncSession) -> int:
    """Total affiché dans la navigation : **les conversations proposées, et elles seules**.

    Les propositions n'y entrent plus depuis le MOD-14 : elles sont publiées d'emblée, et
    aucune ne peut plus attendre. Compter une file qui ne se remplit jamais aurait donné
    un compteur toujours à zéro pour moitié, et surtout une pastille qui ne mènerait
    nulle part — l'écran qui listait ces propositions a été retiré avec elles.
    """
    conversations = await session.scalar(
        select(func.count(Conversation.id)).where(
            Conversation.moderation_status == ModerationStatus.pending
        )
    )
    return conversations or 0


async def moderate(
    session: AsyncSession, statement: Statement, moderator: User, *, approve: bool
) -> Statement:
    statement.moderation_status = (
        ModerationStatus.approved if approve else ModerationStatus.rejected
    )
    statement.moderated_by_user_id = moderator.id
    statement.moderated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(statement)
    return statement


def accepts_statements(conversation: Conversation) -> bool:
    return (
        conversation.state is ConversationState.open
        and conversation.allow_participant_statements
        and conversation.moderation_status is ModerationStatus.approved
    )


def is_published(conversation: Conversation) -> bool:
    """Visible du public : approuvée, publique, et sortie du brouillon."""
    return (
        conversation.moderation_status is ModerationStatus.approved
        and conversation.is_public
        and conversation.state is not ConversationState.draft
    )
