"""Inbound webhooks. Verify, queue, answer fast: the worker does the real work."""

import hmac
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from chirpo.logging import get_logger
from chirpo.workers.queue import Enqueue

TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105  (a header name)
MAX_BODY_BYTES = 1_000_000  # Telegram updates are small; Caddy enforces the same limit

router = APIRouter(tags=["webhooks"])
log = get_logger(__name__)


@dataclass(frozen=True)
class TelegramIntake:
    secret: str
    enqueue: Enqueue


@router.post("/webhooks/telegram")
async def telegram_webhook(request: Request) -> Response:
    intake: TelegramIntake | None = getattr(request.app.state, "telegram_intake", None)
    if intake is None:
        raise HTTPException(status_code=404)

    given = request.headers.get(TELEGRAM_SECRET_HEADER, "")
    if not hmac.compare_digest(given.encode(), intake.secret.encode()):
        raise HTTPException(status_code=401)

    try:
        length = int(request.headers.get("content-length") or 0)
    except ValueError:
        raise HTTPException(status_code=400) from None
    if length > MAX_BODY_BYTES:
        raise HTTPException(status_code=413)
    try:
        raw: Any = await request.json()
    except ValueError:
        raw = None
    update_id = raw.get("update_id") if isinstance(raw, dict) else None
    if not isinstance(update_id, int):
        # Acknowledge garbage: a non-2xx answer would make Telegram retry it forever.
        log.warning("telegram_webhook_malformed")
        return Response(status_code=200)

    try:
        await intake.enqueue(raw, update_id)
    except Exception:
        # 503 makes Telegram retry later, so the update is not lost while Redis is down.
        log.warning("telegram_webhook_enqueue_failed", exc_info=True)
        raise HTTPException(status_code=503) from None
    return Response(status_code=200)
