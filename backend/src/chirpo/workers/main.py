from typing import Any, ClassVar

from chirpo.config import get_settings
from chirpo.logging import bound_context, configure_logging, get_logger, init_sentry

log = get_logger(__name__)


async def noop(ctx: dict[str, Any]) -> str:
    with bound_context(job_id=ctx.get("job_id")):
        log.info("noop_job_ran")
    return "ok"


async def startup(_ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings)
    init_sentry(settings)
    log.info("worker_started")


class WorkerSettings:
    """arq worker definition. The Redis connection is passed in by `chirpo worker`."""

    functions: ClassVar[list[Any]] = [noop]
    on_startup = startup
