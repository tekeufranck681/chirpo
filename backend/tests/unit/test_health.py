import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from chirpo.api.app import create_app
from chirpo.api.health import Check
from chirpo.config import Settings
from chirpo.security import generate_key


async def _ok() -> None:
    return None


async def _down() -> None:
    raise ConnectionError("postgresql+asyncpg://chirpo:HUNTER2@db:5432/chirpo refused")


async def _hangs() -> None:
    await asyncio.sleep(10)


@pytest.fixture
def app() -> FastAPI:
    return create_app(Settings(_env_file=None, encryption_key=generate_key()))


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    # ASGITransport does not run the lifespan, so tests set app.state.checks themselves.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _set_checks(app: FastAPI, **checks: Check) -> None:
    app.state.checks = checks


async def test_health_needs_no_dependencies(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_when_everything_is_up(app: FastAPI, client: httpx.AsyncClient) -> None:
    _set_checks(app, database=_ok, redis=_ok)
    response = await client.get("/api/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


async def test_not_ready_when_a_dependency_is_down(app: FastAPI, client: httpx.AsyncClient) -> None:
    _set_checks(app, database=_down, redis=_ok)
    response = await client.get("/api/ready")
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "unavailable", "redis": "ok"}
    assert "HUNTER2" not in response.text


async def test_slow_dependency_counts_as_down(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("chirpo.api.health.CHECK_TIMEOUT_SECONDS", 0.01)
    _set_checks(app, redis=_hangs)
    response = await client.get("/api/ready")
    assert response.status_code == 503


async def test_docs_live_under_api(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/openapi.json")).status_code == 200
    assert (await client.get("/docs")).status_code == 404


async def test_lifespan_registers_checks_and_closes_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    class FakeEngine:
        async def dispose(self) -> None:
            closed.append("engine")

    class FakeRedis:
        async def aclose(self) -> None:
            closed.append("redis")

    monkeypatch.setattr("chirpo.api.app.create_engine", lambda _settings: FakeEngine())
    monkeypatch.setattr("chirpo.api.app.Redis.from_url", lambda _url: FakeRedis())
    app = create_app(Settings(_env_file=None, encryption_key=generate_key()))

    async with app.router.lifespan_context(app):
        assert set(app.state.checks) == {"database", "redis"}
    assert sorted(closed) == ["engine", "redis"]


async def test_checks_run_in_parallel(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("chirpo.api.health.CHECK_TIMEOUT_SECONDS", 0.2)
    _set_checks(app, database=_hangs, redis=_hangs)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await client.get("/api/ready")
    assert loop.time() - started < 0.35


def test_docs_are_hidden_in_production() -> None:
    app = create_app(Settings(_env_file=None, encryption_key=generate_key(), env="production"))
    assert app.docs_url is None
    assert app.openapi_url is None
