"""Sondes de santé.

Deux sondes distinctes, volontairement :
  - /health    : vivacité. Ne touche pas la base, donc répond même base coupée.
  - /health/db : disponibilité réelle. Fait un SELECT 1.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.config import settings
from app.db import get_session

router = APIRouter(tags=["health"])


# `api_route` et non `get` : FastAPI prend la liste des méthodes au pied de la
# lettre — contrairement à Starlette, il n'ajoute PAS HEAD aux routes GET. Une
# sonde de supervision en HEAD recevrait sinon 405 et conclurait à une panne.
@router.api_route("/health", methods=["GET", "HEAD"])
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "chantier-c-api",
        "version": __version__,
        "environment": settings.environment,
    }


# Même raison que ci-dessus : une sonde de supervision configurée en HEAD — c'est
# le défaut de plusieurs services d'« uptime monitoring » — recevait 405 sur cette
# route et alertait en permanence sur un service parfaitement sain.
@router.api_route("/health/db", methods=["GET", "HEAD"])
async def health_db(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    await session.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}
