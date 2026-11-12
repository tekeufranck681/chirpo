"""GET /metrics and the middleware that counts requests (D57).

/metrics is outside /api on purpose: Caddy forwards only /api and /webhooks, so the numbers are
reachable inside the Docker network (Prometheus) but never from the internet.
"""

import asyncio
import math
from time import perf_counter

from fastapi import APIRouter, Request, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chirpo import metrics
from chirpo.logging import get_logger

QUEUE_READ_SECONDS = 2.0  # a half-open Redis connection must not hang the scrape

router = APIRouter(include_in_schema=False)
log = get_logger(__name__)


@router.get("/metrics")
async def scrape(request: Request) -> Response:
    read_queue = getattr(request.app.state, "queue_length", None)
    if read_queue is not None:
        try:
            metrics.QUEUE_LENGTH.set(await asyncio.wait_for(read_queue(), QUEUE_READ_SECONDS))
        except Exception:
            # Redis trouble must not hide every other number, nor leave the last value looking
            # healthy: NaN shows as a gap. Details go to the logs only.
            metrics.QUEUE_LENGTH.set(math.nan)
            log.warning("queue_length_unreadable", exc_info=True)
    return Response(metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")


class RequestMetrics:
    """Counts requests by route *template* ("/webhooks/telegram"), so paths a scanner makes up
    all land on "unmatched" and can't grow the number of series."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 500  # stays 500 when the app raises before answering
        started = perf_counter()

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            metrics.record_request(
                scope["method"], _template(scope), status, perf_counter() - started
            )


def _template(scope: Scope) -> str:
    """The route's path with its prefix ("/api/health"). FastAPI keeps an included router's
    prefix out of `scope["route"].path`, so the full one comes from the effective route context;
    the plain route path is the fallback."""
    context = scope.get("fastapi", {}).get("effective_route_context")
    full = getattr(context, "path_format", None)
    return full or getattr(scope.get("route"), "path", None) or "unmatched"
