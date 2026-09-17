"""Journal des actions soumises à un plafond.

Persisté en base plutôt que compté en mémoire : un compteur en mémoire repartirait
de zéro à chaque redémarrage — donc à chaque `--reload` en développement — et ne
tiendrait pas si l'API passait à plusieurs processus. Une ligne par tentative, comptée
sur une fenêtre glissante.
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RateLimitHit(Base):
    __tablename__ = "rate_limit_hit"
    __table_args__ = (
        Index("ix_rate_limit_hit_bucket_created", "bucket", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Identifiant du plafond, p. ex. « reset:alice@exemple.fr » ou « reset-ip:1.2.3.4 ».
    bucket: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
