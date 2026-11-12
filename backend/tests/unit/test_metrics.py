"""Metrics (M5.9, D57): what is counted, that labels stay small, that /metrics stays private."""

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from arq.worker import Retry
from fastapi import FastAPI

from chirpo import metrics
from chirpo.adapters.llm.metered import MeteredLLM
from chirpo.api.app import create_app
from chirpo.config import Settings
from chirpo.core.errors import LLMRateLimitedError
from chirpo.core.llm import Interpretation, InterpretRequest
from chirpo.security import generate_key

ROOT = Path(__file__).resolve().parents[3]


def sample(name: str, **labels: str) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


# ------------------------------------------------------------------------------------ jobs


async def test_a_job_that_finishes_is_counted_with_its_duration() -> None:
    @metrics.counted_job
    async def sample_job_ok(ctx: dict[str, object], number: int) -> int:
        return number + 1

    before = sample("chirpo_jobs_total", function="sample_job_ok", result="ok")
    assert await sample_job_ok({}, 1) == 2
    assert sample("chirpo_jobs_total", function="sample_job_ok", result="ok") == before + 1
    assert sample("chirpo_job_seconds_count", function="sample_job_ok") >= 1


async def test_a_job_that_raises_is_counted_and_still_raises() -> None:
    @metrics.counted_job
    async def sample_job_bad(ctx: dict[str, object]) -> None:
        raise RuntimeError("boom")

    before = sample("chirpo_jobs_total", function="sample_job_bad", result="error")
    with pytest.raises(RuntimeError):
        await sample_job_bad({})
    assert sample("chirpo_jobs_total", function="sample_job_bad", result="error") == before + 1


async def test_a_cancelled_job_is_counted_as_cancelled() -> None:
    @metrics.counted_job
    async def sample_job_slow(ctx: dict[str, object]) -> None:
        await asyncio.sleep(10)

    before = sample("chirpo_jobs_total", function="sample_job_slow", result="cancelled")
    task = asyncio.create_task(sample_job_slow({}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sample("chirpo_jobs_total", function="sample_job_slow", result="cancelled") == before + 1


async def test_a_planned_retry_is_not_counted_as_an_error() -> None:
    @metrics.counted_job
    async def sample_job_retry(ctx: dict[str, object]) -> None:
        raise Retry(defer=5)

    before = sample("chirpo_jobs_total", function="sample_job_retry", result="retry")
    with pytest.raises(Retry):
        await sample_job_retry({})
    assert sample("chirpo_jobs_total", function="sample_job_retry", result="retry") == before + 1
    assert sample("chirpo_jobs_total", function="sample_job_retry", result="error") == 0


def test_a_counted_job_keeps_its_name_for_arq() -> None:
    @metrics.counted_job
    async def named_job(ctx: dict[str, object]) -> None:
        return None

    assert named_job.__name__ == "named_job"


# ------------------------------------------------------------------------------------- llm


class Provider:
    model = "model-a"

    def __init__(self, outcome: Interpretation | Exception) -> None:
        self.outcome = outcome

    async def interpret(self, request: InterpretRequest) -> Interpretation:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


REQUEST = InterpretRequest(instructions="", message="", tools=())


async def test_an_llm_call_counts_the_call_and_its_tokens() -> None:
    answer = Interpretation(proposals=(), model="model-a", input_tokens=120, output_tokens=30)
    labels = {"provider": "gemini", "model": "model-a"}
    before_calls = sample("chirpo_llm_calls_total", result="ok", **labels)
    before_in = sample("chirpo_llm_tokens_total", direction="input", **labels)
    before_out = sample("chirpo_llm_tokens_total", direction="output", **labels)
    llm = MeteredLLM(Provider(answer), "gemini")
    assert await llm.interpret(REQUEST) is answer
    assert sample("chirpo_llm_calls_total", result="ok", **labels) == before_calls + 1
    assert sample("chirpo_llm_tokens_total", direction="input", **labels) == before_in + 120
    assert sample("chirpo_llm_tokens_total", direction="output", **labels) == before_out + 30


async def test_a_failed_llm_call_is_counted_by_error_kind_and_still_raises() -> None:
    labels = {"provider": "gemini", "model": "model-a", "result": "LLMRateLimitedError"}
    before = sample("chirpo_llm_calls_total", **labels)
    llm = MeteredLLM(Provider(LLMRateLimitedError("quota")), "gemini")
    with pytest.raises(LLMRateLimitedError):
        await llm.interpret(REQUEST)
    assert sample("chirpo_llm_calls_total", **labels) == before + 1


async def test_an_error_that_is_not_ours_is_counted_as_other() -> None:
    labels = {"provider": "gemini", "model": "model-a", "result": "other"}
    before = sample("chirpo_llm_calls_total", **labels)
    with pytest.raises(KeyError):
        await MeteredLLM(Provider(KeyError("x")), "gemini").interpret(REQUEST)
    assert sample("chirpo_llm_calls_total", **labels) == before + 1


def test_copying_the_wrapper_does_not_recurse() -> None:
    import copy

    llm = MeteredLLM(Provider(Interpretation(proposals=(), model="model-a")), "gemini")
    assert copy.copy(llm).model == "model-a"


def test_the_wrapper_shows_the_model_and_other_attributes_of_the_provider() -> None:
    provider = Provider(Interpretation(proposals=(), model="model-a"))
    provider.thinking = "low"  # type: ignore[attr-defined]
    llm = MeteredLLM(provider, "gemini")
    assert llm.model == "model-a"
    assert llm.thinking == "low"  # type: ignore[attr-defined]


# -------------------------------------------------------------------------------------- api


@pytest.fixture
def app() -> FastAPI:
    return create_app(Settings(_env_file=None, encryption_key=generate_key()))


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_metrics_are_served_in_prometheus_text_format(client: httpx.AsyncClient) -> None:
    await client.get("/api/health")  # something to show
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'chirpo_http_requests_total{method="GET",route="/api/health",status="200"}' in (
        response.text
    )


async def test_requests_are_counted_by_route_template_and_status(
    client: httpx.AsyncClient,
) -> None:
    labels = {"method": "GET", "route": "/api/health", "status": "200"}
    before = sample("chirpo_http_requests_total", **labels)
    await client.get("/api/health")
    assert sample("chirpo_http_requests_total", **labels) == before + 1


async def test_unknown_paths_share_one_label_so_scanners_cannot_grow_the_metrics(
    client: httpx.AsyncClient,
) -> None:
    labels = {"method": "GET", "route": "unmatched", "status": "404"}
    before = sample("chirpo_http_requests_total", **labels)
    for path in ("/wp-login.php", "/.env", "/a/b/c/1234"):
        await client.get(path)
    assert sample("chirpo_http_requests_total", **labels) == before + 3


async def test_an_unusual_http_method_is_counted_as_other(client: httpx.AsyncClient) -> None:
    labels = {"method": "OTHER", "route": "/api/health", "status": "405"}
    before = sample("chirpo_http_requests_total", **labels)
    await client.request("FOOBAR", "/api/health")
    assert sample("chirpo_http_requests_total", **labels) == before + 1
    assert (
        metrics.REGISTRY.get_sample_value(
            "chirpo_http_requests_total",
            {"method": "FOOBAR", "route": "/api/health", "status": "405"},
        )
        is None
    )


async def test_the_queue_length_is_read_when_metrics_are_scraped(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    async def queue_length() -> int:
        return 7

    app.state.queue_length = queue_length
    assert "chirpo_queue_length 7.0" in (await client.get("/metrics")).text


async def test_a_queue_that_can_not_be_read_does_not_break_the_scrape(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    async def broken() -> int:
        raise ConnectionError("redis://:HUNTER2@redis:6379 refused")

    async def seven() -> int:
        return 7

    app.state.queue_length = seven
    assert "chirpo_queue_length 7.0" in (await client.get("/metrics")).text
    app.state.queue_length = broken
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "HUNTER2" not in response.text
    assert "chirpo_queue_length NaN" in response.text  # not the old 7: a dead Redis can't look fine


async def test_a_queue_read_that_hangs_gives_up(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hangs() -> int:
        await asyncio.sleep(30)
        return 1

    monkeypatch.setattr("chirpo.api.metrics.QUEUE_READ_SECONDS", 0.05)
    app.state.queue_length = hangs
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "chirpo_queue_length NaN" in response.text


async def test_metrics_can_be_switched_off() -> None:
    off = create_app(Settings(_env_file=None, encryption_key=generate_key(), metrics_enabled=False))
    transport = httpx.ASGITransport(app=off)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/metrics")).status_code == 404


# ----------------------------------------------------------------------------- worker server


async def test_the_worker_serves_its_metrics_on_its_own_port() -> None:
    server = metrics.serve("127.0.0.1", 0)
    assert server is not None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{server.server_port}/metrics")
        assert "chirpo_jobs_total" in response.text
    finally:
        server.shutdown()
        server.server_close()


def test_a_taken_port_does_not_stop_the_worker() -> None:
    first = metrics.serve("127.0.0.1", 0)
    assert first is not None
    try:
        assert metrics.serve("127.0.0.1", first.server_port) is None
    finally:
        first.shutdown()
        first.server_close()


# ---------------------------------------------------------------------------------- safety

ALLOWED_LABELS = {
    "method",
    "route",
    "status",
    "function",
    "result",
    "provider",
    "model",
    "direction",
}


def test_labels_are_a_small_fixed_set_never_user_text_or_ids() -> None:
    names = {
        label
        for collector in metrics.REGISTRY._names_to_collectors.values()
        for label in getattr(collector, "_labelnames", ())
    }
    assert names <= ALLOWED_LABELS, sorted(names - ALLOWED_LABELS)


def test_caddy_does_not_forward_metrics_to_the_internet() -> None:
    caddyfile = (ROOT / "deploy" / "caddy" / "Caddyfile").read_text()
    forwarded = re.findall(r"handle\s+(/\S+)", caddyfile)
    assert forwarded == ["/api/*", "/webhooks/*"]
    assert "@metrics path /metrics*" in caddyfile
    assert "respond @metrics 404" in caddyfile
    denied_first = caddyfile.index("respond @metrics 404") < caddyfile.index("reverse_proxy")
    assert denied_first


def test_prometheus_scrapes_the_ports_compose_fixes() -> None:
    compose = (ROOT / "docker-compose.dev.yml").read_text()
    prometheus = (ROOT / "deploy/monitoring/prometheus.yml").read_text()
    pinned = re.search(r"^\s+CHIRPO_METRICS_PORT:\s*(\d+)\s*$", compose, re.MULTILINE)
    assert pinned, "compose must fix the worker's metrics port (it beats .env)"
    assert f"worker:{pinned.group(1)}" in prometheus
    assert re.search(r"\bapi:8000\b", prometheus)
    assert "--host" in compose and "8000" not in re.findall(r"--port\D+(\d+)", compose)


def test_the_dashboard_only_uses_metrics_that_exist() -> None:
    dashboard = (ROOT / "deploy/monitoring/grafana/dashboards/chirpo.json").read_text()
    json.loads(dashboard)
    suffix = re.compile(r"_(total|bucket|count|sum|created)$")
    used = {suffix.sub("", name) for name in re.findall(r"chirpo_[a-z_]+", dashboard)}
    defined = {suffix.sub("", name) for name in metrics.REGISTRY._names_to_collectors}
    assert used <= defined, sorted(used - defined)
