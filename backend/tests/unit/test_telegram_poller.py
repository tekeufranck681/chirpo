import asyncio
from typing import Any

from chirpo.adapters.messaging.telegram.poller import run_poller
from chirpo.core.errors import MessagingRateLimitedError, MessagingUnavailableError
from tests.fakes import FakePollCursors


class ScriptedTelegram:
    """Returns one scripted response per fetch; sets `stop` when the script runs out."""

    bot_id = 7000000001

    def __init__(self, script: list[list[dict[str, Any]] | Exception], stop: asyncio.Event) -> None:
        self.script = script
        self.stop = stop
        self.offsets: list[int | None] = []

    async def fetch_updates(self, offset: int | None, wait_seconds: int) -> list[dict[str, Any]]:
        self.offsets.append(offset)
        step = self.script.pop(0)
        if not self.script:
            self.stop.set()
        if isinstance(step, Exception):
            raise step
        return step


async def _run(script: list[Any], cursors: FakePollCursors | None = None) -> tuple[Any, ...]:
    stop = asyncio.Event()
    telegram = ScriptedTelegram(script, stop)
    cursors = cursors or FakePollCursors()
    queued: list[int] = []
    slept: list[float] = []

    async def enqueue(raw: dict[str, Any], update_id: int) -> None:
        queued.append(update_id)

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    await run_poller(telegram, cursors, enqueue, stop=stop, sleep=sleep)  # type: ignore[arg-type]
    return telegram, cursors, queued, slept


async def test_updates_are_queued_in_order_and_the_offset_advances() -> None:
    telegram, cursors, queued, _ = await _run(
        [[{"update_id": 10}, {"update_id": 11}], [{"update_id": 12}]]
    )
    assert queued == [10, 11, 12]
    assert telegram.offsets == [None, 12]
    assert cursors.values[("telegram", "7000000001")] == "13"


async def test_restarts_from_the_saved_offset() -> None:
    cursors = FakePollCursors()
    cursors.values[("telegram", "7000000001")] = "50"
    telegram, *_ = await _run([[]], cursors)
    assert telegram.offsets == [50]


async def test_queue_outage_is_retried_without_losing_the_update() -> None:
    stop = asyncio.Event()
    telegram = ScriptedTelegram([[{"update_id": 10}], [{"update_id": 10}]], stop)
    cursors = FakePollCursors()
    attempts: list[int] = []
    slept: list[float] = []

    async def flaky_enqueue(raw: dict[str, Any], update_id: int) -> None:
        attempts.append(update_id)
        if len(attempts) == 1:
            raise ConnectionError("redis down")

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    await run_poller(telegram, cursors, flaky_enqueue, stop=stop, sleep=sleep)  # type: ignore[arg-type]
    assert attempts == [10, 10]
    assert telegram.offsets == [None, None]
    assert cursors.values[("telegram", "7000000001")] == "11"
    assert slept == [1.0]


async def test_database_outage_while_saving_is_retried() -> None:
    class FlakyCursors(FakePollCursors):
        failed = False

        async def set(self, source: str, scope: str, cursor: str) -> None:
            if not self.failed:
                self.failed = True
                raise OSError("postgres down")
            await super().set(source, scope, cursor)

    _, cursors, queued, slept = await _run(
        [[{"update_id": 10}], [{"update_id": 10}]], FlakyCursors()
    )
    assert queued == [10, 10]
    assert cursors.values[("telegram", "7000000001")] == "11"
    assert slept == [1.0]


async def test_offset_never_moves_backwards() -> None:
    _, cursors, *_ = await _run([[{"update_id": 12}, {"update_id": 11}]])
    assert cursors.values[("telegram", "7000000001")] == "13"


async def test_network_errors_back_off_exponentially() -> None:
    down = MessagingUnavailableError("down")
    *_, slept = await _run([down, down, down, []])
    assert slept == [1.0, 2.0, 4.0]


async def test_rate_limit_waits_as_told() -> None:
    *_, slept = await _run([MessagingRateLimitedError(7), []])
    assert slept == [7]
