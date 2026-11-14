from typing import Any

import pytest

from chirpo.core import messages
from chirpo.workers.main import WorkerSettings, handle_update, noop
from tests.fakes import FakeMessaging, FakeProcessedEvents, make_message


class ParsingMessaging(FakeMessaging):
    """Parses a raw dict of {"text": ...} into a message, like a real adapter would."""

    def parse_update(self, raw: Any) -> Any:
        return make_message(raw["text"], event_id=str(raw["update_id"])) if raw else None


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {"messaging": {"fake": ParsingMessaging()}, "processed": FakeProcessedEvents()}


async def test_noop_job_returns_ok() -> None:
    assert await noop({"job_id": "job-1"}) == "ok"


def test_worker_registers_its_jobs() -> None:
    assert noop in WorkerSettings.functions
    assert handle_update in WorkerSettings.functions


async def test_update_is_answered(ctx: dict[str, Any]) -> None:
    await handle_update(ctx, "fake", {"update_id": 1, "text": "/ping"})
    assert [text for _, text, _ in ctx["messaging"]["fake"].sent] == [messages.PONG]


async def test_duplicate_delivery_is_answered_once(ctx: dict[str, Any]) -> None:
    for _ in range(3):
        await handle_update(ctx, "fake", {"update_id": 1, "text": "/ping"})
    assert len(ctx["messaging"]["fake"].sent) == 1


async def test_unparseable_update_is_ignored(ctx: dict[str, Any]) -> None:
    await handle_update(ctx, "fake", {})
    assert ctx["messaging"]["fake"].sent == []


async def test_unknown_platform_fails_the_job_visibly(ctx: dict[str, Any]) -> None:
    with pytest.raises(RuntimeError, match="slack"):
        await handle_update(ctx, "slack", {"update_id": 1, "text": "/ping"})


async def test_connect_retries_while_telegram_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chirpo.adapters.messaging.connect import connect_messaging
    from chirpo.adapters.messaging.telegram.adapter import TelegramAdapter
    from chirpo.config import Settings
    from chirpo.core.errors import MessagingUnavailableError
    from chirpo.security import generate_key

    calls: list[str] = []
    connected = ParsingMessaging()
    connected.platform = "telegram"

    async def flaky_connect(token: str) -> Any:
        calls.append(token)
        if len(calls) < 3:
            raise MessagingUnavailableError("network down")
        return connected

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(TelegramAdapter, "connect", flaky_connect)
    monkeypatch.setattr("chirpo.adapters.messaging.connect.asyncio.sleep", no_sleep)
    settings = Settings(_env_file=None, encryption_key=generate_key(), telegram_bot_token="1:T")

    assert await connect_messaging(settings) == {"telegram": connected}
    assert len(calls) == 3


async def test_worker_runs_without_telegram_configured() -> None:
    from chirpo.adapters.messaging.connect import connect_messaging
    from chirpo.config import Settings
    from chirpo.security import generate_key

    assert await connect_messaging(Settings(_env_file=None, encryption_key=generate_key())) == {}


def test_the_buffer_is_pruned_every_hour() -> None:
    from chirpo.workers.main import prune_message_buffer

    [job] = [job for job in WorkerSettings.cron_jobs if job.coroutine is prune_message_buffer]
    assert job.minute == 7
    assert job.hour is None


async def test_pruning_the_buffer() -> None:
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from chirpo.workers.main import prune_message_buffer
    from tests.fakes import FakeMessageBuffer

    buffer = FakeMessageBuffer(timedelta(hours=1))
    old = make_message("old", sent_at=datetime.now(UTC) - timedelta(hours=2))
    await buffer.remember(uuid4(), old)
    assert await prune_message_buffer({"buffer": buffer}) == 1


async def test_pruning_without_a_buffer_does_nothing() -> None:
    from chirpo.workers.main import prune_message_buffer

    assert await prune_message_buffer({}) == 0


def test_cards_are_tidied_every_five_minutes() -> None:
    from chirpo.workers.main import expire_pending

    [job] = [job for job in WorkerSettings.cron_jobs if job.coroutine is expire_pending]
    assert job.minute == set(range(0, 60, 5))
    assert job.hour is None


async def test_tidying_the_cards() -> None:
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from uuid import uuid4

    from chirpo.core.actions import CloseIssue, IssueRef
    from chirpo.core.domain import RepoRef
    from chirpo.core.pending import PendingStatus, Step
    from chirpo.workers.main import expire_pending
    from tests.fakes import CHAT, FakeMessaging, FakePendingActions

    repo = RepoRef.parse("github", "tekeufranck681/chirpo-sandbox")
    store = FakePendingActions()
    row = await store.create(
        workspace_id=uuid4(),
        requested_by=uuid4(),
        chat=CHAT,
        steps=[Step.of(CloseIssue(repo=repo.full_name, issue=IssueRef(number=1)), repo)],
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await store.attach_message(row.workspace_id, row.id, "77")
    messaging = FakeMessaging()
    ctx = {"services": SimpleNamespace(pending=store), "messaging": {CHAT.platform: messaging}}
    assert await expire_pending(ctx) == 1
    assert store.rows[row.id].status == PendingStatus.EXPIRED
    assert len(messaging.edited) == 1


async def test_tidying_without_github_configured_does_nothing() -> None:
    from chirpo.workers.main import expire_pending

    assert await expire_pending({"services": None}) == 0


def test_summaries_are_checked_every_minute() -> None:
    from chirpo.workers.main import send_summaries

    [job] = [job for job in WorkerSettings.cron_jobs if job.coroutine is send_summaries]
    assert (job.second, job.minute, job.hour) == (5, None, None)


async def test_no_summaries_without_github() -> None:
    from chirpo.workers.main import send_summaries

    assert await send_summaries({"services": None, "messaging": {}}) == 0
