"""Prometheus metrics (M5.9, D57). Recorded at the edges (HTTP, jobs, LLM adapter), never in core/.

Each process keeps its own numbers: the API serves them on /metrics, the worker on its own port.
Labels are a small fixed set (route templates, function names, error kinds), never message text,
chat ids or workspace ids.
"""

import asyncio
import functools
from collections.abc import Callable, Coroutine
from socketserver import BaseServer
from time import perf_counter
from typing import Any

from arq.worker import Retry
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.exposition import start_http_server
from prometheus_client.process_collector import ProcessCollector

REGISTRY = CollectorRegistry()
ProcessCollector(registry=REGISTRY)

_SECONDS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)

HTTP_REQUESTS = Counter(
    "chirpo_http_requests",
    "HTTP requests by method, route template and status.",
    ["method", "route", "status"],
    registry=REGISTRY,
)
HTTP_SECONDS = Histogram(
    "chirpo_http_request_seconds",
    "HTTP request duration by route template.",
    ["route"],
    buckets=_SECONDS,
    registry=REGISTRY,
)
JOBS = Counter(
    "chirpo_jobs",
    "Worker jobs by function and result (ok, error, cancelled, retry).",
    ["function", "result"],
    registry=REGISTRY,
)
JOB_SECONDS = Histogram(
    "chirpo_job_seconds",
    "Worker job duration by function.",
    ["function"],
    buckets=_SECONDS,
    registry=REGISTRY,
)
LLM_CALLS = Counter(
    "chirpo_llm_calls",
    "LLM calls by provider, model and result (ok, or the kind of error).",
    ["provider", "model", "result"],
    registry=REGISTRY,
)
LLM_TOKENS = Counter(
    "chirpo_llm_tokens",
    "LLM tokens by provider, model and direction (input, output).",
    ["provider", "model", "direction"],
    registry=REGISTRY,
)
QUEUE_LENGTH = Gauge(
    "chirpo_queue_length",
    "Jobs waiting in the queue, read when the API is scraped.",
    registry=REGISTRY,
)

# Anything else (a client can send any token) shares one label, so the series stay few.
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def record_request(method: str, route: str, status: int, seconds: float) -> None:
    HTTP_REQUESTS.labels(method if method in _METHODS else "OTHER", route, str(status)).inc()
    HTTP_SECONDS.labels(route).observe(seconds)


def render() -> bytes:
    return generate_latest(REGISTRY)


def counted_job[F: Callable[..., Coroutine[Any, Any, Any]]](job: F) -> F:
    """Counts how a worker job ended and how long it took. Keeps the job's name and signature
    (arq reads both), so it is typed as returning the same function."""

    @functools.wraps(job)
    async def run(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        started = perf_counter()
        result = "error"
        try:
            value = await job(ctx, *args, **kwargs)
            result = "ok"
            return value
        except asyncio.CancelledError:
            result = "cancelled"
            raise
        except Retry:  # a planned re-run, not a failure
            result = "retry"
            raise
        finally:
            JOBS.labels(job.__name__, result).inc()
            JOB_SECONDS.labels(job.__name__).observe(perf_counter() - started)

    return run  # type: ignore[return-value]


def serve(host: str, port: int) -> BaseServer | None:
    """Starts the metrics server in a background thread (the worker has no web server).
    A taken port returns None: losing metrics must not stop the worker."""
    try:
        server, _thread = start_http_server(port, addr=host, registry=REGISTRY)
    except OSError:
        return None
    return server
