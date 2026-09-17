"""Modèles SQLAlchemy.

Tout module de modèle doit être importé ici pour qu'Alembic le voie en autogénération.
"""

from app.db import Base
from app.models.analysis import (
    AnalysisRun,
    AnalysisStatus,
    BaseCluster,
    BaseClusterMember,
    ParticipantProjection,
    StatementStat,
)
from app.models.gamification import (
    BADGE_CATALOG,
    FIRST_CONVERSATION,
    FIRST_VOTE,
    Badge,
    UserBadge,
)
from app.models.conversation import (
    Conversation,
    ConversationSource,
    ConversationState,
    ConversationTheme,
    ModerationMode,
    ModerationStatus,
    Statement,
    StatementSource,
)
from app.models.lien import LienVerifie, Verdict
from app.models.moderation import (
    AUTEUR_DE_LA_PROPOSITION,
    AUTEUR_SYSTEME,
    ActeModeration,
    Contestation,
    Reformulation,
    JournalModeration,
    Signalement,
    StatutReformulation,
    StatutSignalement,
    Validation,
    VerdictValidation,
)
from app.models.nommage import GroupNaming, MotifNommage
from app.models.participant import Participant
from app.models.rate_limit import RateLimitHit
from app.models.user import User
from app.models.vote import Vote, VoteValue

__all__ = [
    "AUTEUR_DE_LA_PROPOSITION",
    "AUTEUR_SYSTEME",
    "ActeModeration",
    "AnalysisRun",
    "AnalysisStatus",
    "BADGE_CATALOG",
    "BaseCluster",
    "BaseClusterMember",
    "Badge",
    "Base",
    "FIRST_CONVERSATION",
    "FIRST_VOTE",
    "LienVerifie",
    "Conversation",
    "ConversationSource",
    "ConversationState",
    "ConversationTheme",
    "Contestation",
    "Reformulation",
    "ModerationMode",
    "ModerationStatus",
    "GroupNaming",
    "JournalModeration",
    "MotifNommage",
    "Participant",
    "RateLimitHit",
    "Signalement",
    "StatutReformulation",
    "StatutSignalement",
    "Validation",
    "VerdictValidation",
    "ParticipantProjection",
    "Statement",
    "StatementSource",
    "StatementStat",
    "User",
    "UserBadge",
    "Verdict",
    "Vote",
    "VoteValue",
]
