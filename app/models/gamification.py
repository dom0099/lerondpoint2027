"""Niveaux et badges.

Le NIVEAU n'est pas stocké : c'est une fonction pure des compteurs
(`app/services/gamification.py`). Une valeur dérivée qu'on stocke finit toujours par
diverger de sa source, et le recalcul ici tient en deux `COUNT` indexés.

Les BADGES, eux, sont stockés : ils marquent un événement daté et non reproductible
(« le premier vote »), que les compteurs ne suffiraient pas à reconstituer si les
règles changeaient.

Niveaux et badges appartiennent au COMPTE, pas au participant : un participant anonyme
non rattaché n'en a pas. La progression suit donc l'indirection déjà en place — les
votes émis en anonyme comptent dès que le participant rejoint un compte.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

FIRST_VOTE = "first_vote"
FIRST_CONVERSATION = "first_conversation"

#: Source unique du catalogue. La migration 0005 en garde une copie littérale — un
#: fichier de migration doit rester un instantané figé, il ne peut pas importer le
#: code applicatif qui, lui, évolue. Un test compare les deux pour éviter la dérive.
BADGE_CATALOG: list[dict[str, str]] = [
    {
        "code": FIRST_VOTE,
        "label": "Premier vote",
        "description": "Décerné au premier vote émis.",
        "icon": "🗳️",
    },
    {
        "code": FIRST_CONVERSATION,
        "label": "Première conversation créée",
        "description": "Décerné quand une conversation que vous avez proposée est publiée.",
        "icon": "💬",
    },
]


class Badge(Base):
    """Catalogue, semé par migration."""

    __tablename__ = "badge"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    icon: Mapped[str] = mapped_column(String(8), nullable=False, default="")


class UserBadge(Base):
    __tablename__ = "user_badge"
    __table_args__ = (
        UniqueConstraint("user_id", "badge_code", name="uq_user_badge"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    badge_code: Mapped[str] = mapped_column(
        ForeignKey("badge.code", ondelete="CASCADE"), nullable=False
    )
    awarded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    badge: Mapped["Badge"] = relationship(lazy="selectin")
