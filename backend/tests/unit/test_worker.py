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
    from chirpo.adapters.messaging.telegram.adapter import TelegramAdapter
    from chirpo.config import Settings
    from chirpo.core.errors import MessagingUnavailableError
    from chirpo.security import generate_key
    from chirpo.workers.main import connect_messaging

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
    monkeypatch.setattr("chirpo.workers.main.asyncio.sleep", no_sleep)
    settings = Settings(_env_file=None, encryption_key=generate_key(), telegram_bot_token="1:T")

    assert await connect_messaging(settings) == {"telegram": connected}
    assert len(calls) == 3


async def test_worker_runs_without_telegram_configured() -> None:
    from chirpo.config import Settings
    from chirpo.security import generate_key
    from chirpo.workers.main import connect_messaging

    assert await connect_messaging(Settings(_env_file=None, encryption_key=generate_key())) == {}
