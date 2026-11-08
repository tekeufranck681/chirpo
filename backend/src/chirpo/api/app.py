from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI
from redis.asyncio import Redis

from chirpo import __version__
from chirpo.adapters.messaging.connect import connect_messaging
from chirpo.api.health import database_check, redis_check
from chirpo.api.health import router as health_router
from chirpo.api.webhooks import TelegramIntake
from chirpo.api.webhooks import router as webhooks_router
from chirpo.config import Settings, get_settings
from chirpo.db.session import create_engine
from chirpo.logging import configure_logging, get_logger, init_sentry
from chirpo.workers.queue import update_enqueuer

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings)
        init_sentry(settings)
        async with AsyncExitStack() as cleanup:
            engine = create_engine(settings)
            cleanup.push_async_callback(engine.dispose)
            redis = Redis.from_url(settings.redis_url)
            cleanup.push_async_callback(redis.aclose)
            app.state.checks = {"database": database_check(engine), "redis": redis_check(redis)}
            await _start_telegram_webhook(app, settings, cleanup)
            yield

    public_docs = settings.env != "production"
    app = FastAPI(
        title="chirpo",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if public_docs else None,
        openapi_url="/api/openapi.json" if public_docs else None,
        redoc_url=None,
    )
    app.include_router(health_router, prefix="/api")
    app.include_router(webhooks_router)
    return app


async def _start_telegram_webhook(
    app: FastAPI, settings: Settings, cleanup: AsyncExitStack
) -> None:
    """In webhook mode, accept Telegram updates on /webhooks/telegram.

    If Telegram stays unreachable, startup fails: a silently dead webhook would look healthy.
    """
    app.state.telegram_intake = None
    secret = settings.telegram_webhook_secret
    if settings.telegram_mode != "webhook" or secret is None:
        return
    telegram = (await connect_messaging(settings)).get("telegram")
    if telegram is None:
        return
    cleanup.push_async_callback(telegram.aclose)
    queue = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    cleanup.push_async_callback(queue.aclose)
    app.state.telegram_intake = TelegramIntake(
        secret=secret.get_secret_value(), enqueue=update_enqueuer(queue, telegram)
    )
    log.info("telegram_webhook_ready")
