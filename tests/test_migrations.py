"""Aller-retour des migrations, sur une base jetable.

Ce test existe à cause d'une bêtise : le contrôle `upgrade` → `downgrade` → `upgrade`
avait été fait à la main **sur la base de développement**, dont le `downgrade` de 0004
supprime les tables `vote` et d'analyse. Les données de recette ont été détruites.

Le contrôle est donc automatisé ici, contre une base créée et supprimée pour
l'occasion. Il ne doit plus jamais être lancé à la main ailleurs.
"""

import asyncio
import os
import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import _BASE_URL, _DEV_DATABASE_URL

SCRATCH_DB = "chantierc_migrations"
SCRATCH_URL = f"{_BASE_URL}/{SCRATCH_DB}"

# Même garde-fou que dans conftest : ce module fait DROP DATABASE.
assert SCRATCH_URL != _DEV_DATABASE_URL, "la base jetable coïncide avec la base applicative"
assert SCRATCH_DB not in {"chantierc", "chantierc_test"}


async def _recreate_scratch_database() -> None:
    admin = create_async_engine(f"{_BASE_URL}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{SCRATCH_DB}"'))
    await admin.dispose()


async def _drop_scratch_database() -> None:
    admin = create_async_engine(f"{_BASE_URL}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
    await admin.dispose()


def _alembic(*args: str) -> subprocess.CompletedProcess:
    environment = {**os.environ, "DATABASE_URL": SCRATCH_URL}
    return subprocess.run(
        ["alembic", *args], capture_output=True, text=True, env=environment, check=False
    )


@pytest.fixture
def scratch_database():
    asyncio.run(_recreate_scratch_database())
    yield
    asyncio.run(_drop_scratch_database())


def test_migrations_go_up_down_and_up_again(scratch_database) -> None:
    """Un `downgrade` incomplet (type ENUM laissé derrière, par exemple) ne se voit
    qu'en remontant : c'est le second `upgrade` qui échouerait."""
    up = _alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    down = _alembic("downgrade", "base")
    assert down.returncode == 0, down.stderr

    up_again = _alembic("upgrade", "head")
    assert up_again.returncode == 0, up_again.stderr


def test_revision_identifiers_fit_the_version_column(scratch_database) -> None:
    """`alembic_version.version_num` est un varchar(32).

    Un identifiant plus long fait échouer la migration APRÈS l'exécution du DDL, avec
    un message trompeur (« value too long for type character varying(32) ») qui ne
    nomme ni la colonne ni la révision fautive.
    """
    import pathlib

    trop_longs = []
    for fichier in pathlib.Path("migrations/versions").glob("*.py"):
        for ligne in fichier.read_text().splitlines():
            if ligne.startswith("revision: str = "):
                identifiant = ligne.split('"')[1]
                if len(identifiant) > 32:
                    trop_longs.append((identifiant, len(identifiant)))

    assert trop_longs == []
