"""Integration fixtures: a fresh `<db>_test` database migrated once per run, and a session per
test inside a transaction that is rolled back. Skipped without Postgres locally; failing in CI."""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from chirpo.config import Settings
from chirpo.security import generate_key

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _test_database_url() -> str:
    settings = Settings(encryption_key=generate_key())
    url = make_url(settings.database_url)
    if not url.database:
        raise ValueError("CHIRPO_DATABASE_URL must include a database name")
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


def alembic_config(database_url: str) -> Config:
    config = Config(ALEMBIC_INI)
    config.attributes["database_url"] = database_url
    config.attributes["configure_logger"] = False
    return config


async def _recreate_database(database_url: str) -> None:
    url = make_url(database_url)
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    # Identifiers can't be bind parameters; the name comes from our settings, not user input.
    quoted = '"' + str(url.database).replace('"', '""') + '"'
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)"))
            await connection.execute(text(f"CREATE DATABASE {quoted}"))
    finally:
        await admin.dispose()


@pytest.fixture(scope="session")
async def database_url() -> str:
    url = _test_database_url()
    try:
        await _recreate_database(url)
    except (OSError, SQLAlchemyError) as error:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"Postgres not reachable ({type(error).__name__}); run: docker compose up -d")
    # env.py calls asyncio.run(), which can't nest inside this running loop.
    await asyncio.to_thread(command.upgrade, alembic_config(url), "head")
    return url


@pytest.fixture
def alembic_cfg(database_url: str) -> Config:
    return alembic_config(database_url)


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
