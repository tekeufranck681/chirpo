"""Long-polling loop: fetch updates, enqueue each one, then save the position.

The position (offset) is saved only after an update is queued, so a crash re-fetches it rather than
losing it; the worker's duplicate check makes the repeat harmless (D31).
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from chirpo.adapters.messaging.telegram.adapter import PLATFORM, TelegramAdapter
from chirpo.core.errors import MessagingRateLimitedError
from chirpo.core.ports import PollCursors
from chirpo.logging import get_logger

Enqueue = Callable[[dict[str, Any], int], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]

POLL_TIMEOUT_SECONDS = 30
MAX_BACKOFF_SECONDS = 60.0
WEBHOOK_HINT = (
    "a webhook is registered for this bot; remove it with `chirpo telegram delete-webhook`"
)

log = get_logger(__name__)


async def run_poller(
    adapter: TelegramAdapter,
    cursors: PollCursors,
    enqueue: Enqueue,
    *,
    stop: asyncio.Event | None = None,
    sleep: Sleep = asyncio.sleep,
) -> None:
    scope = str(adapter.bot_id)
    saved = await cursors.get(PLATFORM, scope)
    offset = int(saved) if saved else None
    backoff = 1.0
    log.info("telegram_polling_started", offset=offset)

    while stop is None or not stop.is_set():
        try:
            updates = await adapter.fetch_updates(offset, POLL_TIMEOUT_SECONDS)
            for raw in updates:
                update_id = int(raw["update_id"])
                await enqueue(raw, update_id)
                offset = max(offset or 0, update_id + 1)
                await cursors.set(PLATFORM, scope, str(offset))
        except MessagingRateLimitedError as error:
            await sleep(error.retry_after_seconds)
            continue
        except Exception as error:
            # Telegram, Redis or Postgres trouble: the offset only moves after a successful
            # enqueue + save, so retrying later re-fetches whatever was not stored.
            hint = WEBHOOK_HINT if "webhook" in str(error).lower() else None
            log.warning("telegram_poll_failed", error=str(error), retry_in=backoff, hint=hint)
            await sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            continue
        backoff = 1.0
