from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from chirpo.api.app import create_app
from chirpo.api.webhooks import TELEGRAM_SECRET_HEADER, TelegramIntake
from chirpo.config import Settings
from chirpo.security import generate_key

SECRET = "s3cret_token-abc"
UPDATE = {"update_id": 900001, "message": {"message_id": 1, "text": "/ping"}}


class Queue:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.jobs: list[tuple[dict[str, Any], int]] = []

    async def __call__(self, raw: dict[str, Any], update_id: int) -> None:
        if self.fail:
            raise ConnectionError("redis down")
        self.jobs.append((raw, update_id))


@pytest.fixture
def app() -> FastAPI:
    return create_app(Settings(_env_file=None, encryption_key=generate_key()))


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _accept(app: FastAPI, queue: Queue) -> None:
    app.state.telegram_intake = TelegramIntake(secret=SECRET, enqueue=queue)


async def _post(client: httpx.AsyncClient, body: Any, secret: str | None = SECRET) -> int:
    headers = {TELEGRAM_SECRET_HEADER: secret} if secret is not None else {}
    response = await client.post("/webhooks/telegram", json=body, headers=headers)
    return response.status_code


async def test_valid_update_is_queued(app: FastAPI, client: httpx.AsyncClient) -> None:
    queue = Queue()
    _accept(app, queue)
    assert await _post(client, UPDATE) == 200
    assert queue.jobs == [(UPDATE, 900001)]


@pytest.mark.parametrize("secret", [None, "", "wrong", SECRET + "x"])
async def test_wrong_or_missing_secret_is_rejected(
    app: FastAPI, client: httpx.AsyncClient, secret: str | None
) -> None:
    queue = Queue()
    _accept(app, queue)
    assert await _post(client, UPDATE, secret=secret) == 401
    assert queue.jobs == []


async def test_disabled_outside_webhook_mode(app: FastAPI, client: httpx.AsyncClient) -> None:
    app.state.telegram_intake = None
    assert await _post(client, UPDATE) == 404


@pytest.mark.parametrize("body", [{}, {"update_id": "x"}, [1, 2], "text"])
async def test_malformed_body_is_acknowledged_and_dropped(
    app: FastAPI, client: httpx.AsyncClient, body: Any
) -> None:
    queue = Queue()
    _accept(app, queue)
    assert await _post(client, body) == 200
    assert queue.jobs == []


async def test_non_json_body_is_acknowledged(app: FastAPI, client: httpx.AsyncClient) -> None:
    _accept(app, Queue())
    response = await client.post(
        "/webhooks/telegram", content=b"not json", headers={TELEGRAM_SECRET_HEADER: SECRET}
    )
    assert response.status_code == 200


async def test_queue_outage_asks_telegram_to_retry(app: FastAPI, client: httpx.AsyncClient) -> None:
    _accept(app, Queue(fail=True))
    assert await _post(client, UPDATE) == 503


def _webhook_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "encryption_key": generate_key(),
        "telegram_bot_token": "1:T",
        "telegram_mode": "webhook",
        "public_url": "https://chirpo.example.com",
        "telegram_webhook_secret": SECRET,
        **overrides,
    }
    return Settings(_env_file=None, **values)


async def test_webhook_mode_connects_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import AsyncExitStack

    from chirpo.api import app as app_module
    from tests.fakes import FakeMessaging

    closed: list[str] = []

    class Telegram(FakeMessaging):
        platform = "telegram"

        async def aclose(self) -> None:
            closed.append("telegram")

    class Pool:
        async def aclose(self) -> None:
            closed.append("queue")

    async def connect(_settings: Settings) -> dict[str, Any]:
        return {"telegram": Telegram()}

    async def create_pool(_settings: Any) -> Pool:
        return Pool()

    monkeypatch.setattr(app_module, "connect_messaging", connect)
    monkeypatch.setattr(app_module, "create_pool", create_pool)
    app = create_app(_webhook_settings())

    async with AsyncExitStack() as cleanup:
        await app_module._start_telegram_webhook(app, _webhook_settings(), cleanup)
        assert app.state.telegram_intake.secret == SECRET
    assert sorted(closed) == ["queue", "telegram"]


async def test_unreachable_telegram_fails_startup_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import AsyncExitStack

    from chirpo.api import app as app_module
    from chirpo.core.errors import MessagingUnavailableError

    async def connect(_settings: Settings) -> dict[str, Any]:
        raise MessagingUnavailableError("telegram down")

    monkeypatch.setattr(app_module, "connect_messaging", connect)
    app = create_app(_webhook_settings())
    with pytest.raises(MessagingUnavailableError):
        async with AsyncExitStack() as cleanup:
            await app_module._start_telegram_webhook(app, _webhook_settings(), cleanup)


async def test_polling_mode_never_opens_the_webhook() -> None:
    from contextlib import AsyncExitStack

    from chirpo.api.app import _start_telegram_webhook

    app = create_app(_webhook_settings(telegram_mode="polling"))
    async with AsyncExitStack() as cleanup:
        await _start_telegram_webhook(app, _webhook_settings(telegram_mode="polling"), cleanup)
    assert app.state.telegram_intake is None


async def test_bad_secret_is_rejected_before_the_body_is_read(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    _accept(app, Queue())
    assert await _post(client, "garbage", secret="wrong") == 401


async def test_oversized_body_is_refused(app: FastAPI, client: httpx.AsyncClient) -> None:
    _accept(app, Queue())
    response = await client.post(
        "/webhooks/telegram",
        content=b"{}",
        headers={TELEGRAM_SECRET_HEADER: SECRET, "content-length": "2000000"},
    )
    assert response.status_code == 413


async def test_update_job_id_combines_bot_and_update() -> None:
    from chirpo.workers.queue import update_enqueuer
    from tests.fakes import FakeMessaging

    jobs: list[tuple[Any, ...]] = []

    class Pool:
        async def enqueue_job(self, *args: Any, **kwargs: Any) -> None:
            jobs.append((*args, kwargs["_job_id"]))

    await update_enqueuer(Pool(), FakeMessaging())({"update_id": 5}, 5)  # type: ignore[arg-type]
    assert jobs == [("handle_update", "fake", {"update_id": 5}, "fake:bot:5")]


@pytest.mark.parametrize("secret", ["has space", "x" * 257, "émoji"])
def test_invalid_webhook_secret_is_rejected(secret: str) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="telegram_webhook_secret"):
        Settings(_env_file=None, encryption_key=generate_key(), telegram_webhook_secret=secret)
