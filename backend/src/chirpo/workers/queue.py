"""Queueing work for the worker: inbound updates (poller, webhook) and device-login checks."""

import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from arq.connections import ArqRedis

from chirpo.core.identity import PendingLogin
from chirpo.core.ports import MessagingAdapter
from chirpo.security import Encryptor

Enqueue = Callable[[dict[str, Any], int], Awaitable[None]]
LOGIN_CLAIM_PREFIX = "chirpo:login:"


def update_enqueuer(queue: ArqRedis, messaging: MessagingAdapter) -> Enqueue:
    async def enqueue(raw: dict[str, Any], update_id: int) -> None:
        # A fixed job id: the queue refuses a second copy of the same update (D31).
        job_id = f"{messaging.event_source}:{update_id}"
        await queue.enqueue_job("handle_update", messaging.platform, raw, _job_id=job_id)

    return enqueue


class ArqLoginJobs:
    """Schedules the next device-login check. The device code is encrypted before it is queued, so
    it never appears in Redis or in the worker's job logs in clear."""

    def __init__(self, queue: ArqRedis, encryptor: Encryptor) -> None:
        self._queue = queue
        self._encryptor = encryptor

    async def check_login_later(self, pending: PendingLogin, delay_seconds: int) -> None:
        await self._queue.enqueue_job(
            "check_device_login",
            seal_pending_login(self._encryptor, pending),
            _defer_by=timedelta(seconds=delay_seconds),
        )

    async def claim(self, key: str, token: str, seconds: int) -> bool:
        return bool(await self._queue.set(_claim(key), token, nx=True, ex=seconds))

    async def current(self, key: str) -> str | None:
        value = await self._queue.get(_claim(key))
        return value.decode() if isinstance(value, bytes) else value

    async def replace(self, key: str, token: str, seconds: int) -> None:
        await self._queue.set(_claim(key), token, ex=seconds)

    async def release(self, key: str, token: str) -> None:
        # Not atomic, but the window only matters for a login replaced in the same instant.
        if await self.current(key) == token:
            await self._queue.delete(_claim(key))

    async def hold(self, key: str, seconds: int) -> None:
        await self._queue.expire(_claim(key), seconds)


def _claim(key: str) -> str:
    return f"{LOGIN_CLAIM_PREFIX}{key}"


def seal_pending_login(encryptor: Encryptor, pending: PendingLogin) -> str:
    fields = pending.model_dump(mode="json")
    fields["device_code"] = pending.device_code.get_secret_value()
    return encryptor.encrypt(json.dumps(fields))


def open_pending_login(encryptor: Encryptor, sealed: str) -> PendingLogin:
    return PendingLogin.model_validate_json(encryptor.decrypt(sealed))
