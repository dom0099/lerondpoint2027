"""Votes.

**Convention de signe : `agree = +1`, `disagree = -1`, `pass = 0`.** C'est celle de
red-dwarf, et elle est l'inverse de celle de Pol.is. Le chantier B avait qualifié cette
inversion de « piège le plus coûteux de l'exercice » : des groupes en miroir, sans
qu'aucune erreur ne le signale. En stockant d'emblée dans la convention de l'outil
d'analyse, il n'existe aucune conversion nulle part dans le code.

Une seule ligne par (participant, proposition), mise à jour au re-vote — et non un
journal append-only. C'est directement la forme que `generate_raw_matrix` de red-dwarf
attend : son `pivot()` casse sur les doublons, ce qui avait obligé le chantier B à
passer par `votes_latest_unique` côté Pol.is.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class VoteValue(enum.IntEnum):
    agree = 1
    disagree = -1
    pass_ = 0

    @classmethod
    def values(cls) -> set[int]:
        return {member.value for member in cls}


class Vote(Base):
    __tablename__ = "vote"
    __table_args__ = (
        UniqueConstraint("participant_id", "statement_id", name="uq_vote_participant_statement"),
        Index("ix_vote_statement_id", "statement_id"),
        Index("ix_vote_participant_id", "participant_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participant.id", ondelete="CASCADE"), nullable=False
    )
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("statement.id", ondelete="CASCADE"), nullable=False
    )
    value: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    modified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    statement: Mapped["Statement"] = relationship()  # noqa: F821

    def __repr__(self) -> str:
        return f"<Vote p{self.participant_id} s{self.statement_id} = {self.value}>"
