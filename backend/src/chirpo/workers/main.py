import asyncio
from typing import Any, ClassVar

from arq import cron

from chirpo.config import Settings, get_settings
from chirpo.core.errors import MessagingError
from chirpo.core.pipeline import process
from chirpo.core.ports import MessagingAdapter
from chirpo.db.repositories import SqlProcessedEvents
from chirpo.db.session import create_engine, create_session_factory
from chirpo.logging import bound_context, configure_logging, get_logger, init_sentry

log = get_logger(__name__)

CONNECT_ATTEMPTS = 5
PROCESSED_EVENTS_RETENTION_DAYS = 7


async def noop(ctx: dict[str, Any]) -> str:
    with bound_context(job_id=ctx.get("job_id")):
        log.info("noop_job_ran")
    return "ok"


async def handle_update(ctx: dict[str, Any], platform: str, raw: dict[str, Any]) -> None:
    messaging: MessagingAdapter | None = ctx["messaging"].get(platform)
    with bound_context(job_id=ctx.get("job_id"), platform=platform):
        if messaging is None:
            # Fail visibly: returning would silently drop an update the poller already moved past.
            raise RuntimeError(f"no messaging adapter configured for {platform!r}")
        event = messaging.parse_update(raw)
        if event is None:
            return
        with bound_context(event_id=event.event_id):
            handled = await process(event, messaging, ctx["processed"])
            log.info("update_handled" if handled else "duplicate_update_skipped")


async def prune_processed_events(ctx: dict[str, Any]) -> int:
    deleted: int = await ctx["processed"].prune(older_than_days=PROCESSED_EVENTS_RETENTION_DAYS)
    log.info("processed_events_pruned", deleted=deleted)
    return deleted


async def connect_messaging(settings: Settings) -> dict[str, MessagingAdapter]:
    if settings.telegram_bot_token is None:
        log.warning("telegram_not_configured", hint="set CHIRPO_TELEGRAM_BOT_TOKEN in .env")
        return {}
    from chirpo.adapters.messaging.telegram.adapter import TelegramAdapter

    token = settings.telegram_bot_token.get_secret_value()
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            telegram = await TelegramAdapter.connect(token)
        except MessagingError as error:
            if attempt == CONNECT_ATTEMPTS:
                raise
            log.warning("telegram_connect_failed", error=str(error), attempt=attempt)
            await asyncio.sleep(2**attempt)
        else:
            return {telegram.platform: telegram}
    raise AssertionError("unreachable")


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings)
    init_sentry(settings)
    ctx["messaging"] = await connect_messaging(settings)
    ctx["engine"] = create_engine(settings)
    ctx["processed"] = SqlProcessedEvents(create_session_factory(ctx["engine"]))
    log.info("worker_started", platforms=sorted(ctx["messaging"]))


async def shutdown(ctx: dict[str, Any]) -> None:
    for adapter in ctx.get("messaging", {}).values():
        await adapter.aclose()
    if "engine" in ctx:
        await ctx["engine"].dispose()


class WorkerSettings:
    """arq worker definition. The Redis connection is passed in by `chirpo worker`."""

    functions: ClassVar[list[Any]] = [noop, handle_update]
    # Telegram never redelivers updates this old, so their duplicate-check rows can go.
    cron_jobs: ClassVar[list[Any]] = [cron(prune_processed_events, hour=3, minute=17)]
    on_startup = startup
    on_shutdown = shutdown
