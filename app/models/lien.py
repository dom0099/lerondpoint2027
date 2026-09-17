"""L'état de vérification d'une adresse publiée par le site (chantier K).

**Une ligne par ADRESSE, et non par lien.** Les adresses vivent dans deux tables —
`statement_source` pour les apports des participants, `conversation_source` pour les
chiffres du modérateur — et la même adresse peut apparaître dans les deux, plusieurs
fois. C'est déjà le cas : deux chiffres du débat sur le permis citent la même page.

Des colonnes de vérification sur chacune des deux tables auraient fait interroger deux
fois le même site pour la même page, à des instants différents, avec le risque
d'afficher deux verdicts contradictoires sur la même adresse dans le même écran.
"""

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Verdict(str, enum.Enum):
    """Le dernier classement observé pour une adresse.

    `injoignable` est le SEUL qui puisse aboutir à un signalement public — et encore
    faut-il qu'il ait été observé deux fois, à une semaine d'intervalle (voir
    `LienVerifie.signale`).
    """

    non_verifie = "non_verifie"        # jamais interrogée
    joignable = "joignable"            # 2xx ou 3xx
    injoignable = "injoignable"        # 404 ou 410 — la page a disparu
    non_concluant = "non_concluant"    # 403, 429, 5xx, délai, DNS : on ne sait pas
    hors_perimetre = "hors_perimetre"  # domaine absent de la liste blanche


class LienVerifie(Base):
    __tablename__ = "lien_verifie"

    id: Mapped[int] = mapped_column(primary_key=True)

    #: SHA-256 de l'adresse normalisée, en hexadécimal. C'est LUI qui porte l'unicité,
    #: et non l'adresse : un index B-tree PostgreSQL refuse une entrée de plus de
    #: ~2 700 octets, or `url` fait 2 000 caractères — jusqu'à 8 000 octets en UTF-8.
    #: Une contrainte d'unicité posée sur `url` aurait donc tenu en recette et cédé le
    #: jour où quelqu'un colle une adresse à rallonge.
    empreinte: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    #: Le domaine en ASCII, minuscule, tel qu'il sert à la liste blanche. Stocké parce
    #: que la sélection des adresses à revoir s'en sert, et qu'un recalcul par ligne
    #: coûterait un décodage IDNA pour rien.
    domaine: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    verdict: Mapped[Verdict] = mapped_column(
        Enum(Verdict, name="verdict_lien"),
        default=Verdict.non_verifie,
        nullable=False,
        index=True,
    )
    code_http: Mapped[int | None] = mapped_column(Integer, nullable=True)

    dernier_essai: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    dernier_succes: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Début de la série d'échecs en cours. Remis à None dès qu'une réponse aboutit.
    #: C'est cette date, et non `dernier_essai`, qui dit depuis quand une page est
    #: introuvable — et donc si l'on a assez attendu pour le dire au public.
    premier_echec: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    echecs_consecutifs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<LienVerifie {self.domaine} {self.verdict.value}>"
