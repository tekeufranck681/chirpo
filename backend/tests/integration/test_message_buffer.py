"""The message buffer: the same tests run on the in-memory fake and on PostgreSQL."""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from chirpo.core.domain import ChatRef, ChatUser, InboundMessage
from chirpo.core.llm import MAX_MESSAGE_CHARS
from chirpo.core.ports import MessageBuffer, Workspaces
from chirpo.db import models as db
from chirpo.db.repositories import SqlMessageBuffer, SqlWorkspaces
from chirpo.db.session import create_session_factory
from chirpo.security import Encryptor, generate_key
from tests.fakes import FakeMessageBuffer, FakeWorkspaces

RETENTION = timedelta(hours=24)
FRANCK = ChatUser(platform="telegram", user_id="11", display_name="Franck", username="franck")


@dataclass
class Stores:
    workspaces: Workspaces
    buffer: MessageBuffer


@pytest.fixture(params=["fake", "postgres"])
def stores(request: pytest.FixtureRequest) -> Iterator[Stores]:
    if request.param == "fake":
        yield Stores(FakeWorkspaces(), FakeMessageBuffer(RETENTION))
        return
    engine: AsyncEngine = request.getfixturevalue("engine")
    sessions = create_session_factory(engine)
    encryptor = Encryptor([generate_key()])
    yield Stores(SqlWorkspaces(sessions), SqlMessageBuffer(sessions, encryptor, RETENTION))


def new_chat() -> ChatRef:
    return ChatRef(platform="telegram", chat_id=f"-100{uuid.uuid4().int % 10**10}")


_ids = iter(range(1, 10**9))


def message(chat: ChatRef, text: str, **overrides: Any) -> InboundMessage:
    number = next(_ids)
    values: dict[str, Any] = {
        "event_id": str(number),
        "chat": chat,
        "sender": FRANCK,
        "message_id": str(number),
        "text": text,
        "sent_at": datetime.now(UTC),
        **overrides,
    }
    return InboundMessage(**values)


async def workspace_for(stores: Stores, chat: ChatRef) -> uuid.UUID:
    return (await stores.workspaces.get_or_create_for_chat(chat, name="Team")).id


async def test_remembers_and_reads_back(stores: Stores) -> None:
    chat = new_chat()
    workspace = await workspace_for(stores, chat)
    sent = message(chat, "checkout crashes on Android 13", reply_to_message_id="7")
    await stores.buffer.remember(workspace, sent)

    [recent] = await stores.buffer.recent(workspace, chat)
    assert recent.message_id == sent.message_id
    assert recent.reply_to_message_id == "7"
    assert recent.sender == FRANCK
    assert recent.text == "checkout crashes on Android 13"
    assert recent.sent_at == sent.sent_at


async def test_the_last_messages_oldest_first(stores: Stores) -> None:
    chat = new_chat()
    workspace = await workspace_for(stores, chat)
    start = datetime.now(UTC) - timedelta(minutes=10)
    for minute in range(5):
        text = f"message {minute}"
        await stores.buffer.remember(
            workspace, message(chat, text, sent_at=start + timedelta(minutes=minute))
        )
    recent = await stores.buffer.recent(workspace, chat, limit=3)
    assert [entry.text for entry in recent] == ["message 2", "message 3", "message 4"]


async def test_scoped_to_the_workspace_and_chat(stores: Stores) -> None:
    chat, other_chat = new_chat(), new_chat()
    workspace = await workspace_for(stores, chat)
    other_workspace = await workspace_for(stores, other_chat)
    await stores.buffer.remember(workspace, message(chat, "ours"))
    await stores.buffer.remember(other_workspace, message(other_chat, "theirs"))

    assert [entry.text for entry in await stores.buffer.recent(workspace, chat)] == ["ours"]
    assert await stores.buffer.recent(workspace, other_chat) == []
    assert await stores.buffer.recent(other_workspace, chat) == []


async def test_a_redelivered_message_is_kept_once(stores: Stores) -> None:
    chat = new_chat()
    workspace = await workspace_for(stores, chat)
    sent = message(chat, "once")
    await stores.buffer.remember(workspace, sent)
    await stores.buffer.remember(workspace, sent)
    assert len(await stores.buffer.recent(workspace, chat)) == 1


async def test_long_text_is_cut(stores: Stores) -> None:
    chat = new_chat()
    workspace = await workspace_for(stores, chat)
    await stores.buffer.remember(workspace, message(chat, "x" * (MAX_MESSAGE_CHARS + 500)))
    [recent] = await stores.buffer.recent(workspace, chat)
    assert len(recent.text) == MAX_MESSAGE_CHARS


async def test_old_messages_are_never_read_and_get_pruned(stores: Stores) -> None:
    chat = new_chat()
    workspace = await workspace_for(stores, chat)
    old = datetime.now(UTC) - RETENTION - timedelta(minutes=5)
    await stores.buffer.remember(workspace, message(chat, "yesterday's", sent_at=old))
    await stores.buffer.remember(workspace, message(chat, "today's"))

    assert [entry.text for entry in await stores.buffer.recent(workspace, chat)] == ["today's"]
    assert await stores.buffer.prune() >= 1
    assert [entry.text for entry in await stores.buffer.recent(workspace, chat)] == ["today's"]


async def test_text_is_encrypted_at_rest(engine: AsyncEngine) -> None:
    sessions = create_session_factory(engine)
    chat = new_chat()
    workspace = (await SqlWorkspaces(sessions).get_or_create_for_chat(chat, name="Team")).id
    encryptor = Encryptor([generate_key()])
    buffer = SqlMessageBuffer(sessions, encryptor, RETENTION)
    await buffer.remember(workspace, message(chat, "the secret launch plan"))
    async with sessions() as session:
        stored = await session.scalar(
            select(db.BufferedMessage.text_encrypted).where(
                db.BufferedMessage.workspace_id == workspace
            )
        )
    assert stored is not None
    assert "launch" not in stored
    assert encryptor.decrypt(stored) == "the secret launch plan"


async def test_a_future_date_is_treated_as_now(stores: Stores) -> None:
    chat = new_chat()
    workspace = await workspace_for(stores, chat)
    future = datetime.now(UTC) + timedelta(days=365)
    await stores.buffer.remember(workspace, message(chat, "from the future", sent_at=future))
    await stores.buffer.remember(workspace, message(chat, "now"))
    [first, second] = await stores.buffer.recent(workspace, chat)
    assert first.text == "from the future"
    assert first.sent_at <= datetime.now(UTC)
    assert second.text == "now"


async def test_same_second_keeps_arrival_order(stores: Stores) -> None:
    chat = new_chat()
    workspace = await workspace_for(stores, chat)
    second = datetime.now(UTC).replace(microsecond=0)
    for text in ("first", "second", "third"):
        await stores.buffer.remember(workspace, message(chat, text, sent_at=second))
    assert [m.text for m in await stores.buffer.recent(workspace, chat, limit=2)] == [
        "second",
        "third",
    ]


async def test_topics_are_kept_apart(stores: Stores) -> None:
    chat = new_chat()
    topic_a = chat.model_copy(update={"thread_id": "20"})
    topic_b = chat.model_copy(update={"thread_id": "30"})
    workspace = await workspace_for(stores, chat)
    await stores.buffer.remember(workspace, message(topic_a, "in topic A"))
    await stores.buffer.remember(workspace, message(topic_b, "in topic B"))
    assert [m.text for m in await stores.buffer.recent(workspace, topic_a)] == ["in topic A"]
    assert [m.text for m in await stores.buffer.recent(workspace, chat)] == []


async def test_no_limit_means_nothing(stores: Stores) -> None:
    chat = new_chat()
    workspace = await workspace_for(stores, chat)
    await stores.buffer.remember(workspace, message(chat, "hi"))
    assert await stores.buffer.recent(workspace, chat, limit=0) == []


async def test_messages_from_a_removed_key_are_skipped(engine: AsyncEngine) -> None:
    sessions = create_session_factory(engine)
    chat = new_chat()
    workspace = (await SqlWorkspaces(sessions).get_or_create_for_chat(chat, name="Team")).id
    old_key, new_key = generate_key(), generate_key()
    await SqlMessageBuffer(sessions, Encryptor([old_key]), RETENTION).remember(
        workspace, message(chat, "written with the old key")
    )
    rotated = SqlMessageBuffer(sessions, Encryptor([new_key]), RETENTION)
    await rotated.remember(workspace, message(chat, "written with the new key"))
    assert [m.text for m in await rotated.recent(workspace, chat)] == ["written with the new key"]


async def test_zero_retention_prunes_everything(engine: AsyncEngine) -> None:
    sessions = create_session_factory(engine)
    chat = new_chat()
    workspace = (await SqlWorkspaces(sessions).get_or_create_for_chat(chat, name="Team")).id
    encryptor = Encryptor([generate_key()])
    await SqlMessageBuffer(sessions, encryptor, RETENTION).remember(workspace, message(chat, "hi"))
    off = SqlMessageBuffer(sessions, encryptor, timedelta(0))
    assert await off.prune() >= 1
    assert await SqlMessageBuffer(sessions, encryptor, RETENTION).recent(workspace, chat) == []


async def test_forgetting_a_workspace(stores: Stores) -> None:
    chat, other_chat = new_chat(), new_chat()
    workspace = await workspace_for(stores, chat)
    other = await workspace_for(stores, other_chat)
    await stores.buffer.remember(workspace, message(chat, "ours"))
    await stores.buffer.remember(other, message(other_chat, "theirs"))
    assert await stores.buffer.forget(workspace) == 1
    assert await stores.buffer.recent(workspace, chat) == []
    assert [m.text for m in await stores.buffer.recent(other, other_chat)] == ["theirs"]


async def test_find_one_message(stores: Stores) -> None:
    chat = new_chat()
    workspace = await workspace_for(stores, chat)
    sent = message(chat, "the one replied to")
    await stores.buffer.remember(workspace, sent)
    found = await stores.buffer.find(workspace, chat, sent.message_id)
    assert found is not None
    assert found.text == "the one replied to"
    assert await stores.buffer.find(workspace, chat, "nope") is None
    assert await stores.buffer.find(workspace, new_chat(), sent.message_id) is None
