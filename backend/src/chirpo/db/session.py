"""One engine per process; short-lived sessions (one per request or job)."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from chirpo.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(settings.database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # Avoids implicit reloads after commit, which fail outside an async context.
    return async_sessionmaker(engine, expire_on_commit=False)
