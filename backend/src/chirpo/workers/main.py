from typing import Any, ClassVar

from arq import cron, func

from chirpo.adapters.codehost.github.device import GitHubDeviceLogin
from chirpo.adapters.messaging.connect import connect_messaging
from chirpo.config import Settings, get_settings
from chirpo.core.identity import IdentityLinking
from chirpo.core.pipeline import process
from chirpo.core.ports import MessagingAdapter
from chirpo.db.repositories import SqlAuditLog, SqlMembers, SqlProcessedEvents, SqlWorkspaces
from chirpo.db.session import create_engine, create_session_factory
from chirpo.logging import bound_context, configure_logging, get_logger, init_sentry
from chirpo.security import Encryptor
from chirpo.workers.queue import ArqLoginJobs, open_pending_login

log = get_logger(__name__)

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
            handled = await process(event, messaging, ctx["processed"], ctx.get("identity"))
            log.info("update_handled" if handled else "duplicate_update_skipped")


async def check_device_login(ctx: dict[str, Any], sealed: str) -> None:
    identity: IdentityLinking | None = ctx.get("identity")
    with bound_context(job_id=ctx.get("job_id")):
        if identity is None:
            log.warning("device_login_not_configured")
            return
        pending = open_pending_login(ctx["encryptor"], sealed)
        messaging: MessagingAdapter | None = ctx["messaging"].get(pending.chat.platform)
        if messaging is None:
            raise RuntimeError(f"no messaging adapter configured for {pending.chat.platform!r}")
        await identity.check(pending, messaging)


async def prune_processed_events(ctx: dict[str, Any]) -> int:
    deleted: int = await ctx["processed"].prune(older_than_days=PROCESSED_EVENTS_RETENTION_DAYS)
    log.info("processed_events_pruned", deleted=deleted)
    return deleted


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings)
    init_sentry(settings)
    ctx["messaging"] = await connect_messaging(settings)
    ctx["engine"] = create_engine(settings)
    ctx["processed"] = SqlProcessedEvents(create_session_factory(ctx["engine"]))
    ctx["encryptor"] = Encryptor(settings.encryption_keys)
    ctx["identity"] = _identity_linking(ctx, settings)
    log.info(
        "worker_started",
        platforms=sorted(ctx["messaging"]),
        github_login=ctx["identity"] is not None,
    )


def _identity_linking(ctx: dict[str, Any], settings: Settings) -> IdentityLinking | None:
    if settings.github_app_client_id is None:
        return None
    sessions = create_session_factory(ctx["engine"])
    ctx["device_login"] = GitHubDeviceLogin(settings.github_app_client_id)
    return IdentityLinking(
        login=ctx["device_login"],
        workspaces=SqlWorkspaces(sessions),
        members=SqlMembers(sessions),
        audit=SqlAuditLog(sessions),
        jobs=ArqLoginJobs(ctx["redis"], ctx["encryptor"]),
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    for adapter in ctx.get("messaging", {}).values():
        await adapter.aclose()
    if "device_login" in ctx:
        await ctx["device_login"].aclose()
    if "engine" in ctx:
        await ctx["engine"].dispose()


class WorkerSettings:
    """arq worker definition. The Redis connection is passed in by `chirpo worker`."""

    functions: ClassVar[list[Any]] = [
        noop,
        handle_update,
        # It reschedules itself: an arq retry would start a second polling chain. No stored result.
        func(check_device_login, max_tries=1, keep_result=0),
    ]
    # Telegram never redelivers updates this old, so their duplicate-check rows can go.
    cron_jobs: ClassVar[list[Any]] = [cron(prune_processed_events, hour=3, minute=17)]
    on_startup = startup
    on_shutdown = shutdown
