"""Compte participant.

Le compte est OPTIONNEL en v1 et deviendra obligatoire plus tard. Les votes ne
pointent jamais ici directement : ils pointent vers `Participant`, qui peut exister
sans compte. Voir app/models/participant.py.
"""

import uuid
from datetime import datetime

from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTableUUID
from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(SQLAlchemyBaseUserTableUUID, Base):
    """Colonnes héritées de fastapi-users : id, email, hashed_password,
    is_active, is_superuser, is_verified."""

    __tablename__ = "user"

    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: La description libre du profil. Facultative comme `display_name` — et pour la
    #: même raison : le compte lui-même est facultatif, rien ici ne peut devenir un
    #: passage obligé.
    bio: Mapped[str | None] = mapped_column(String(280), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    participant: Mapped["Participant | None"] = relationship(  # noqa: F821
        back_populates="user", uselist=False
    )

    def __repr__(self) -> str:
        return f"<User {self.email}>"


__all__ = ["User", "uuid"]
