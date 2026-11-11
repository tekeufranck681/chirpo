from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
from arq import cron, func

from chirpo.adapters.codehost.github.adapter import GitHubAdapter
from chirpo.adapters.codehost.github.auth import GitHubAuth
from chirpo.adapters.codehost.github.device import GitHubDeviceLogin
from chirpo.adapters.llm import llm_from_settings
from chirpo.adapters.messaging.connect import connect_messaging
from chirpo.adapters.ratelimit.redis import RedisCounters
from chirpo.config import Settings, get_settings
from chirpo.core.commands import Services
from chirpo.core.errors import CodeHostError
from chirpo.core.identity import IdentityLinking
from chirpo.core.permissions import PermissionChecker
from chirpo.core.pipeline import process
from chirpo.core.ports import MessagingAdapter
from chirpo.core.ratelimit import Limits, RateLimiter
from chirpo.core.sweep import sweep
from chirpo.db.repositories import (
    SqlAuditLog,
    SqlMembers,
    SqlMessageBuffer,
    SqlPendingActions,
    SqlProcessedEvents,
    SqlWorkspaces,
)
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
            handled = await process(event, messaging, ctx["processed"], ctx.get("services"))
            log.info("update_handled" if handled else "duplicate_update_skipped")


async def check_device_login(ctx: dict[str, Any], sealed: str) -> None:
    services: Services | None = ctx.get("services")
    identity = services.identity if services else None
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


async def prune_message_buffer(ctx: dict[str, Any]) -> int:
    buffer = ctx.get("buffer")
    if buffer is None:
        return 0
    deleted: int = await buffer.prune()
    log.info("message_buffer_pruned", deleted=deleted)
    return deleted


async def expire_pending(ctx: dict[str, Any]) -> int:
    """Expires old cards, fails cut-off runs and deletes old finished rows (M5.4)."""
    services = ctx.get("services")
    if services is None:  # GitHub isn't configured, so there are no cards
        return 0
    swept = await sweep(services.pending, ctx["messaging"], datetime.now(UTC))
    return swept.expired + swept.failed + swept.purged


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings)
    init_sentry(settings)
    ctx["messaging"] = await connect_messaging(settings)
    ctx["engine"] = create_engine(settings)
    ctx["processed"] = SqlProcessedEvents(create_session_factory(ctx["engine"]))
    ctx["encryptor"] = Encryptor(settings.encryption_keys)
    # Built even when off (0 hours): the hourly prune then clears what an earlier setting kept.
    ctx["buffer"] = SqlMessageBuffer(
        create_session_factory(ctx["engine"]),
        ctx["encryptor"],
        timedelta(hours=settings.message_buffer_hours),
    )
    ctx["http"] = httpx.AsyncClient()  # shared by outbound calls such as the LLM's (D45)
    ctx["services"] = await _services(ctx, settings)
    services = ctx["services"]
    log.info(
        "worker_started",
        platforms=sorted(ctx["messaging"]),
        github=services is not None,
        github_login=bool(services and services.identity),
        llm=services.llm.model if services and services.llm else None,
    )


async def _services(ctx: dict[str, Any], settings: Settings) -> Services | None:
    """Everything that acts on GitHub; None (commands explain it) when GitHub isn't configured."""
    auth = GitHubAuth.from_settings(settings)
    if auth is None:
        return None
    sessions = create_session_factory(ctx["engine"])
    workspaces, members, audit = (
        SqlWorkspaces(sessions),
        SqlMembers(sessions),
        SqlAuditLog(sessions),
    )
    codehost = GitHubAdapter(auth)
    identity = None
    if settings.github_app_client_id:
        ctx["device_login"] = GitHubDeviceLogin(settings.github_app_client_id)
        identity = IdentityLinking(
            login=ctx["device_login"],
            workspaces=workspaces,
            members=members,
            audit=audit,
            jobs=ArqLoginJobs(ctx["redis"], ctx["encryptor"]),
        )
    return Services(
        workspaces=workspaces,
        members=members,
        audit=audit,
        codehost=codehost,
        permissions=PermissionChecker(codehost),
        pending=SqlPendingActions(sessions),
        identity=identity,
        app_install_url=await _app_install_url(auth),
        buffer=ctx["buffer"] if settings.message_buffer_hours else None,
        context_hours=settings.message_buffer_hours,
        llm=llm_from_settings(settings, client=ctx["http"]),
        read_images=settings.read_images,
        proposal_ttl=timedelta(minutes=settings.proposal_ttl_minutes),
        stale_after=timedelta(minutes=settings.stale_after_minutes),
        limits=RateLimiter(
            RedisCounters(ctx["redis"]),
            Limits(
                llm_per_member_hour=settings.llm_calls_per_member_hour,
                llm_per_workspace_hour=settings.llm_calls_per_workspace_hour,
                llm_tokens_per_day=settings.llm_tokens_per_day,
                github_writes_per_member_hour=settings.github_writes_per_member_hour,
            ),
        ),
    )


async def _app_install_url(auth: GitHubAuth) -> str | None:
    """Where admins install the App on more repos; looked up once, optional."""
    if not auth.is_app:
        return None
    try:
        identity = await auth.whoami()
    except CodeHostError:
        log.warning("github_app_lookup_failed")
        return None
    return f"https://github.com/apps/{identity.name}/installations/new"


async def shutdown(ctx: dict[str, Any]) -> None:
    for adapter in ctx.get("messaging", {}).values():
        await adapter.aclose()
    if "device_login" in ctx:
        await ctx["device_login"].aclose()
    if "http" in ctx:
        await ctx["http"].aclose()
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
    cron_jobs: ClassVar[list[Any]] = [
        cron(prune_processed_events, hour=3, minute=17),
        cron(prune_message_buffer, minute=7),  # hourly
        cron(expire_pending, minute=set(range(0, 60, 5))),
    ]
    on_startup = startup
    on_shutdown = shutdown
