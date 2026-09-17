"""Identité courante.

Répond toujours 200 : avec ou sans compte, vérifié ou non. C'est le contrat que
l'endpoint de vote de C3 reprendra.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_participant
from app.db import get_session
from app.models import Participant
from app.schemas import AccountInfo, BadgeRead, MeResponse, ProgressRead
from app.services import gamification

router = APIRouter(prefix="/api", tags=["me"])


@router.get("/me", response_model=MeResponse)
async def me(
    participant: Participant = Depends(get_current_participant),
    session: AsyncSession = Depends(get_session),
) -> MeResponse:
    account = None
    progress = None
    if participant.user is not None:
        account = AccountInfo(
            email=participant.user.email,
            is_verified=participant.user.is_verified,
            display_name=participant.user.display_name,
            bio=participant.user.bio,
        )
        # Niveaux et badges appartiennent au compte : rien pour un anonyme.
        computed = await gamification.progress_for(session, participant.user)
        progress = ProgressRead(
            votes=computed.votes,
            conversations=computed.conversations,
            points=computed.points,
            level=computed.level,
            label=computed.label,
            next_level_at=computed.next_level_at,
            points_to_next=computed.points_to_next,
            badges=[
                BadgeRead(
                    code=b.code, label=b.label, description=b.description, icon=b.icon
                )
                for b in computed.badges
            ],
        )
    return MeResponse(
        participant_id=participant.id,
        authenticated=account is not None,
        account=account,
        progress=progress,
    )
