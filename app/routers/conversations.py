"""API des conversations, côté participant."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_participant
from app.db import get_session
from app.models import Conversation, ConversationState, ModerationStatus, Participant
from app.schemas import (
    CarteRead,
    ConversationDetail,
    EnveloppeRead,
    GroupRead,
    ConversationSummary,
    PositionRead,
    StatementCreate,
    StatementSubmitted,
    statement_read,
    VisiteurRead,
)
from app.services import carte as carte_service
from app.services import conversations as service
from app.services import groups as groups_service
from app.services import rate_limit
from app.services.liens import LienInvalide, valider_liens
from app.services.liens_verification import signalements
from app.services.reformulation import acceptees_par_proposition

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


async def _published(session: AsyncSession, slug: str) -> Conversation:
    """Une conversation en brouillon n'existe pas pour les participants."""
    conversation = await service.by_slug(session, slug)
    if conversation is None or not service.is_published(conversation):
        raise HTTPException(status_code=404, detail="Conversation inconnue")
    return conversation


@router.get("", response_model=list[ConversationSummary])
async def list_open(session: AsyncSession = Depends(get_session)):
    result = await session.scalars(
        select(Conversation)
        .where(
            Conversation.is_public.is_(True),
            Conversation.state != ConversationState.draft,
            Conversation.moderation_status == ModerationStatus.approved,
        )
        .order_by(Conversation.created_at.desc())
    )
    return [
        ConversationSummary(
            slug=c.slug, title=c.title, description=c.description, state=c.state.value
        )
        for c in result
    ]


@router.get("/{slug}", response_model=ConversationDetail)
async def detail(slug: str, session: AsyncSession = Depends(get_session)):
    conversation = await _published(session, slug)
    statements = await service.visible_statements(session, conversation)
    # Les liens morts de toutes les propositions, en une requête pour la page entière.
    signales = await signalements(
        session, [source.url for s in statements for source in s.sources]
    )
    # Les reformulations acceptées de toutes les propositions, en une requête pour la
    # page entière — même règle que les liens morts juste au-dessus.
    reformulees = await acceptees_par_proposition(session, [s.id for s in statements])
    return ConversationDetail(
        slug=conversation.slug,
        title=conversation.title,
        description=conversation.description,
        state=conversation.state.value,
        allow_participant_statements=service.accepts_statements(conversation),
        statements=[
            statement_read(s, signales, reformulees) for s in statements
        ],
    )


@router.post("/{slug}/statements", response_model=StatementSubmitted, status_code=201)
async def propose_statement(
    slug: str,
    payload: StatementCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    participant: Participant = Depends(get_current_participant),
):
    """Dépôt d'une proposition par un participant.

    Passe par `get_current_participant`, donc ouverte aux anonymes et aux comptes non
    vérifiés — même contrat que le futur endpoint de vote.
    """
    conversation = await _published(session, slug)
    if not service.accepts_statements(conversation):
        raise HTTPException(
            status_code=409,
            detail="Cette conversation n'accepte pas de nouvelles propositions.",
        )

    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Proposition vide.")
    if len(text) > service.MAX_STATEMENT_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"Proposition trop longue (max {service.MAX_STATEMENT_LENGTH}).",
        )

    # Les liens sont contrôlés AVANT le plafond de débit, pour la même raison que le
    # texte : une adresse mal recopiée ne doit pas consommer le quota de la journée.
    # Et avant toute écriture : une proposition enregistrée dont le lien serait ensuite
    # refusé laisserait le participant sans sa source et sans savoir pourquoi.
    try:
        liens = valider_liens([(s.url, s.label) for s in payload.sources])
    except LienInvalide as erreur:
        raise HTTPException(status_code=422, detail=str(erreur)) from None

    # Plafond vérifié après la validation de forme, pour qu'un texte trop long ne
    # consomme pas le quota. Le refus est explicite (429 + message) : il n'y a rien à
    # cacher ici, et un participant doit comprendre pourquoi sa proposition est
    # refusée plutôt que de la retenter en boucle.
    client_ip = request.client.host if request.client else None
    if not await rate_limit.statement_proposal_allowed(
        session, participant.id, conversation.id, client_ip
    ):
        raise HTTPException(
            status_code=429,
            detail=(
                f"Vous avez proposé {rate_limit.STATEMENTS_PER_PARTICIPANT} "
                "propositions dans cette conversation en moins de 24 heures. "
                "Laissez-les être relues, et revenez demain."
            ),
        )

    try:
        statement = await service.submit_statement(
            session, conversation, participant, text, liens
        )
    except service.DuplicateStatement:
        raise HTTPException(
            status_code=409,
            detail="Cette proposition a déjà été proposée dans cette conversation.",
        ) from None
    return StatementSubmitted(
        id=statement.id,
        moderation_status=statement.moderation_status.value,
        visible=statement.moderation_status is ModerationStatus.approved,
    )


@router.get("/{slug}/my-group", response_model=GroupRead | None)
async def my_group(
    slug: str,
    session: AsyncSession = Depends(get_session),
    participant: Participant = Depends(get_current_participant),
):
    """Groupe de la personne courante, ou None si aucun calcul n'a encore abouti."""
    conversation = await _published(session, slug)
    view = await groups_service.for_participant(session, conversation, participant)
    if view is None:
        return None
    return GroupRead(
        name=view.name,
        others=view.others,
        total_participants=view.total_participants,
        group_count=view.group_count,
        computed_at=view.computed_at.isoformat() if view.computed_at else None,
        reason=view.reason,
    )


@router.get("/{slug}/carte", response_model=CarteRead | None)
async def carte(
    slug: str,
    session: AsyncSession = Depends(get_session),
    participant: Participant = Depends(get_current_participant),
):
    """Positions et contours de groupe du dernier calcul abouti.

    Lecture seule, et tout provient d'un seul run : les positions de deux calculs ne
    sont pas dans le même repère et ne doivent jamais être superposées (voir
    `app/services/carte.py`). None tant qu'aucun calcul n'a abouti.
    """
    conversation = await _published(session, slug)
    vue = await carte_service.carte(session, conversation, participant)
    if vue is None:
        return None
    return CarteRead(
        run_id=vue.run_id,
        computed_at=vue.computed_at,
        positions=[PositionRead(**point) for point in vue.positions],
        groupes=[
            EnveloppeRead(name=g.name, size=g.size, sommets=g.sommets)
            for g in vue.groupes
        ],
        visiteur=VisiteurRead(**vars(vue.visiteur)) if vue.visiteur else None,
    )
