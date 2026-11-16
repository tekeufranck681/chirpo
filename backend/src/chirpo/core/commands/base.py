"""What every command handler gets, and the checks they share (who is asking, are they allowed)."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from chirpo.core import messages
from chirpo.core.domain import AuditResult, InboundMessage, RepoRef
from chirpo.core.errors import CodeHostForbiddenError, MessagingError
from chirpo.core.identity import IdentityLinking
from chirpo.core.permissions import Action, PermissionChecker, Refusal, Verdict
from chirpo.core.ports import (
    AuditLog,
    CodeHostAdapter,
    LLMProvider,
    Members,
    MessageBuffer,
    MessagingAdapter,
    PendingActions,
    Workspaces,
)
from chirpo.core.ratelimit import LimitVerdict, RateLimiter
from chirpo.core.workspaces import AuditEntry, Member, Workspace

log = logging.getLogger(__name__)
TYPING_REFRESH_SECONDS = 4.0  # Telegram shows "typing…" for 5 seconds per request


@dataclass(frozen=True, slots=True)
class Services:
    """Everything that needs the code host. None in a Request when GitHub isn't configured."""

    workspaces: Workspaces
    members: Members
    audit: AuditLog
    codehost: CodeHostAdapter
    permissions: PermissionChecker
    pending: PendingActions
    identity: IdentityLinking | None = None
    app_install_url: str | None = None
    buffer: MessageBuffer | None = None  # None: the instance keeps no messages (D46)
    context_hours: int = 0
    llm: LLMProvider | None = None  # None: no plain language, commands only
    limits: RateLimiter | None = None  # None: no rate limits (D49)
    read_images: bool = False  # show images sent with a request to the LLM (D50)
    proposal_ttl: timedelta = timedelta(hours=1)  # how long a card can be confirmed (D53)
    stale_after: timedelta = timedelta(minutes=5)  # older messages get an "offline" note (D54)
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Request:
    message: InboundMessage
    args: str
    messaging: MessagingAdapter
    services: Services | None

    async def reply(self, text: str) -> None:
        await self.messaging.send_message(self.message.chat, text, reply_to=self.message.message_id)


Handler = Callable[[Request], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Actor:
    """The person asking, inside the chat's workspace."""

    services: Services
    workspace: Workspace
    member: Member


async def needs_services(request: Request) -> Services | None:
    if request.services is None:
        await request.reply(messages.GITHUB_NOT_CONFIGURED)
    return request.services


async def find_actor(
    request: Request, *, no_workspace: str = messages.SEND_IAM_HERE
) -> Actor | None:
    """The chat's workspace and the sender's membership; replies `no_workspace` if there is none."""
    services = await needs_services(request)
    if services is None:
        return None
    workspace = await services.workspaces.for_chat(request.message.chat)
    if workspace is None:
        await request.reply(no_workspace)
        return None
    member = await services.members.for_chat_user(workspace.id, request.message.sender)
    return Actor(services, workspace, member)


async def audit(
    actor: Actor,
    action: Action | str,
    result: AuditResult,
    payload: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    await actor.services.audit.record(
        AuditEntry(
            workspace_id=actor.workspace.id,
            actor_member_id=actor.member.id,
            action=str(action),
            result=result,
            payload=payload or {},
            error=error,
        )
    )


async def limited(request: Request, verdict: LimitVerdict) -> bool:
    """True when a rate limit says no. Only the first refusal in a window is answered, so a flood
    can't make the bot flood the chat back."""
    if verdict.allowed:
        return False
    log.info(
        "rate_limited scope=%s retry_after=%d told=%s",
        verdict.scope,
        verdict.retry_after,
        verdict.notify,
    )
    if verdict.notify and verdict.scope:
        texts = {
            "member": messages.RATE_LIMITED_MEMBER,
            "workspace": messages.RATE_LIMITED_TEAM,
            "budget": messages.RATE_LIMITED_BUDGET,
            "write": messages.WRITE_LIMITED,
        }
        wait = messages.duration(verdict.retry_after)
        with suppress(MessagingError):  # a courtesy: the request is refused either way
            await request.reply(texts[verdict.scope].format(wait=wait))
    return True


async def allowed(
    request: Request, actor: Actor, action: Action, repo: RepoRef | None = None
) -> bool:
    """Runs the permission check; on refusal, audits it and tells the person why."""
    payload = {"repo": repo.full_name} if repo else {}
    try:
        verdict = await actor.services.permissions.check(actor.member, action, repo)
    except CodeHostForbiddenError:  # the App can't reach the repo: not installed there
        text = messages.APP_NOT_INSTALLED.format(repo=repo.full_name if repo else "that repo")
        if actor.services.app_install_url:
            text += messages.APP_INSTALL_HINT.format(url=actor.services.app_install_url)
        await audit(actor, action, AuditResult.DENIED, payload, "app_not_installed")
        await request.reply(text)
        return False
    if verdict.allowed:
        return True
    await audit(actor, action, AuditResult.DENIED, payload, verdict.refusal)
    await request.reply(_refusal_text(verdict, actor.member, repo))
    return False


def _refusal_text(verdict: Verdict, member: Member, repo: RepoRef | None) -> str:
    requirement = verdict.requirement
    match verdict.refusal:
        case Refusal.NOT_VERIFIED:
            return messages.LINK_GITHUB_FIRST
        case Refusal.ROLE_TOO_LOW:
            return messages.ROLE_TOO_LOW.format(role=requirement.role)
        case _:
            return messages.NO_REPO_ACCESS.format(
                login=member.codehost.login if member.codehost else "?",
                permission=requirement.codehost,
                repo=repo.full_name if repo else "this repo",
            )


@asynccontextmanager
async def typing_shown(request: Request) -> AsyncIterator[None]:
    """Keeps "typing…" on screen until the reply is sent: chat apps drop it after ~5 seconds,
    while the model can take 10-20, and a silent bot looks broken."""
    messaging, chat = request.messaging, request.message.chat

    async def show() -> None:
        try:
            await messaging.show_typing(chat)
        except MessagingError as error:  # only a courtesy, but say why it's missing
            log.info("typing_failed error=%s", type(error).__name__)

    async def keep_typing() -> None:
        while True:
            await asyncio.sleep(TYPING_REFRESH_SECONDS)
            await show()

    await show()  # right away, not when the loop first gets a turn
    task = asyncio.create_task(keep_typing())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
