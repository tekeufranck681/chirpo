import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from chirpo.db.repositories import SqlPollCursors, SqlProcessedEvents
from chirpo.db.session import create_session_factory

pytestmark = pytest.mark.integration


async def test_an_event_can_be_claimed_only_once(engine: AsyncEngine) -> None:
    events = SqlProcessedEvents(create_session_factory(engine))
    external_id = uuid.uuid4().hex
    assert await events.claim("telegram:1", external_id) is True
    assert await events.claim("telegram:1", external_id) is False
    assert await events.claim("telegram:2", external_id) is True


async def test_poll_cursor_is_created_then_updated(engine: AsyncEngine) -> None:
    cursors = SqlPollCursors(create_session_factory(engine))
    scope = uuid.uuid4().hex
    assert await cursors.get("telegram", scope) is None
    await cursors.set("telegram", scope, "10")
    await cursors.set("telegram", scope, "11")
    assert await cursors.get("telegram", scope) == "11"


async def test_old_processed_events_are_pruned(engine: AsyncEngine) -> None:
    from sqlalchemy import text

    events = SqlProcessedEvents(create_session_factory(engine))
    old, recent = uuid.uuid4().hex, uuid.uuid4().hex
    await events.claim("prune-test", old)
    await events.claim("prune-test", recent)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE processed_events SET received_at = now() - interval '8 days' "
                "WHERE external_id = :id"
            ),
            {"id": old},
        )

    assert await events.prune(older_than_days=7) >= 1
    assert await events.claim("prune-test", old) is True
    assert await events.claim("prune-test", recent) is False
