"""Implementations of the repository Protocols from core.ports."""

from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chirpo.db.models import PollCursor, ProcessedEvent


class SqlProcessedEvents:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def claim(self, source: str, external_id: str) -> bool:
        statement = (
            insert(ProcessedEvent)
            .values(source=source, external_id=external_id)
            .on_conflict_do_nothing(index_elements=["source", "external_id"])
            .returning(ProcessedEvent.id)
        )
        async with self._sessions.begin() as session:
            return (await session.execute(statement)).scalar_one_or_none() is not None

    async def prune(self, older_than_days: int) -> int:
        cutoff = func.now() - timedelta(days=older_than_days)
        statement = delete(ProcessedEvent).where(ProcessedEvent.received_at < cutoff)
        async with self._sessions.begin() as session:
            result = await session.execute(statement)
            return int(result.rowcount)  # type: ignore[attr-defined]


class SqlPollCursors:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, source: str, scope: str) -> str | None:
        statement = select(PollCursor.cursor).where(
            PollCursor.source == source, PollCursor.scope == scope
        )
        async with self._sessions() as session:
            return (await session.execute(statement)).scalar_one_or_none()

    async def set(self, source: str, scope: str, cursor: str) -> None:
        statement = (
            insert(PollCursor)
            .values(source=source, scope=scope, cursor=cursor)
            .on_conflict_do_update(
                index_elements=["source", "scope"],
                set_={"cursor": cursor, "updated_at": func.now()},
            )
        )
        async with self._sessions.begin() as session:
            await session.execute(statement)
