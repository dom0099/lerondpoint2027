"""Participant — l'entité à laquelle les votes se rattachent.

Point central du modèle : un participant peut exister SANS compte (`user_id` NULL),
identifié par un jeton anonyme déposé dans un cookie signé. S'il crée un compte plus
tard, la même ligne `participant` est rattachée au compte, donc **son historique de
votes le suit**. Sans cette indirection, rendre les comptes obligatoires ferait perdre
tous les votes déjà émis en anonyme.

`user_id` est UNIQUE : au plus un participant par compte. C'est nécessaire pour
l'analyse — un compte qui apparaîtrait deux fois dans la matrice de votes fausserait
le clustering.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Participant(Base):
    __tablename__ = "participant"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), unique=True, nullable=True
    )
    anon_token: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    # --- thèmes de prédilection (chantier J) --------------------------------------
    # Rangés ICI et non sur `user`, et c'est la décision structurante du chantier :
    # le compte est facultatif sur ce site, donc une préférence portée par le compte
    # ne vaudrait que pour une minorité. Sur `participant`, elle vaut aussi pour un
    # visiteur anonyme, et elle **survit à la création du compte sans transfert à
    # écrire** — c'est la même ligne qui est rattachée au compte.
    #
    # Réserve, à dire sur l'écran plutôt qu'à laisser découvrir : le cookie anonyme
    # est un cookie. Effacé, ou changé d'appareil, la préférence disparaît. C'est déjà
    # vrai des votes.
    #
    # NULL = jamais réglé, et non « aucun thème » : c'est ce qui distingue quelqu'un
    # qui n'a pas répondu de quelqu'un qui a tout décoché, et donc ce qui décide de
    # montrer ou non l'onglet « Mes centres d'intérêt ».
    #
    # JSON et non ARRAY : le filtre lit ces codes en Python pour en faire un IN sur
    # `conversation_theme`, jamais un opérateur d'ensemble en SQL. Un type natif
    # n'apporterait donc rien qu'une dépendance de plus au dialecte.
    themes: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # lazy="selectin" : chargé avec le participant. En asynchrone, un chargement
    # paresseux différé lèverait MissingGreenlet au moment de l'accès.
    user: Mapped["User | None"] = relationship(  # noqa: F821
        back_populates="participant", lazy="selectin"
    )

    def __repr__(self) -> str:
        kind = "compte" if self.user_id else "anonyme"
        return f"<Participant {self.id} ({kind})>"
