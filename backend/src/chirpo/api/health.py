import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request, Response
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chirpo.logging import get_logger

Check = Callable[[], Awaitable[object]]
CHECK_TIMEOUT_SECONDS = 2.0

router = APIRouter(tags=["health"])
log = get_logger(__name__)


def database_check(engine: AsyncEngine) -> Check:
    async def check() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    return check


def redis_check(redis: Redis) -> Check:
    async def check() -> None:
        await redis.ping()

    return check


async def _run(name: str, check: Check) -> str:
    try:
        await asyncio.wait_for(check(), timeout=CHECK_TIMEOUT_SECONDS)
    except Exception:
        # Details go to the logs only: the response must not reveal hosts or credentials.
        log.warning("readiness_check_failed", check=name, exc_info=True)
        return "unavailable"
    return "ok"


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request, response: Response) -> dict[str, object]:
    checks: dict[str, Check] = request.app.state.checks
    outcomes = await asyncio.gather(*(_run(name, check) for name, check in checks.items()))
    results = dict(zip(checks, outcomes, strict=True))

    is_ready = all(result == "ok" for result in results.values())
    response.status_code = 200 if is_ready else 503
    return {"status": "ok" if is_ready else "unavailable", "checks": results}
