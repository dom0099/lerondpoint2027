"""Accès base : moteur asynchrone, fabrique de sessions, base déclarative.

Le moteur est créé paresseusement pour qu'importer l'application ne suppose pas
une base joignable (les tests unitaires n'en ont pas besoin).
"""

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Base déclarative commune à tous les modèles."""


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    return create_async_engine(settings.database_url, pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dépendance FastAPI : une session par requête."""
    async with get_sessionmaker()() as session:
        yield session
