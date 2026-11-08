from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from chirpo import __version__
from chirpo.api.health import database_check, redis_check
from chirpo.api.health import router as health_router
from chirpo.config import Settings, get_settings
from chirpo.db.session import create_engine
from chirpo.logging import configure_logging, init_sentry


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings)
        init_sentry(settings)
        engine = create_engine(settings)
        redis = Redis.from_url(settings.redis_url)
        app.state.checks = {"database": database_check(engine), "redis": redis_check(redis)}
        try:
            yield
        finally:
            await redis.aclose()
            await engine.dispose()

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
    return app
