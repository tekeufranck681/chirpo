"""Queueing inbound updates for the worker. Shared by the poller and the webhook endpoint."""

from collections.abc import Awaitable, Callable
from typing import Any

from arq.connections import ArqRedis

from chirpo.core.ports import MessagingAdapter

Enqueue = Callable[[dict[str, Any], int], Awaitable[None]]


def update_enqueuer(queue: ArqRedis, messaging: MessagingAdapter) -> Enqueue:
    async def enqueue(raw: dict[str, Any], update_id: int) -> None:
        # A fixed job id: the queue refuses a second copy of the same update (D31).
        job_id = f"{messaging.event_source}:{update_id}"
        await queue.enqueue_job("handle_update", messaging.platform, raw, _job_id=job_id)

    return enqueue
