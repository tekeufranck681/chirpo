"""/iam: prove which code host account is yours, with the code host's device login (D39).

The code is sent privately; a background check waits for the approval without blocking anything.
The code host's answer names the account by its permanent id, which is what permission checks use.
"""

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, SecretStr

from chirpo.core import messages
from chirpo.core.domain import AuditResult, ChatRef, InboundMessage
from chirpo.core.errors import CodeHostError, MessagingForbiddenError
from chirpo.core.ports import AuditLog, Members, MessagingAdapter, Workspaces
from chirpo.core.workspaces import AuditEntry, CodeHostAccount

SLOW_DOWN_EXTRA_SECONDS = 5
# Upper bound for one login (GitHub's codes last 15 minutes), shortened to the code's real lifetime.
LOGIN_CLAIM_SECONDS = 15 * 60
# A new /iam replaces a pending code, but at most once a minute (each code costs GitHub calls).
NEW_CODE_AFTER_SECONDS = 60


class LoginStatus(StrEnum):
    PENDING = "pending"  # not approved yet
    SLOW_DOWN = "slow_down"  # asked too often: wait longer
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class DeviceLogin(BaseModel):
    model_config = ConfigDict(frozen=True)

    device_code: SecretStr  # lets whoever holds it finish the login: never shown or logged
    user_code: str
    verification_url: str
    expires_in_seconds: int
    interval_seconds: int


class LoginCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: LoginStatus
    account: CodeHostAccount | None = None
    interval_seconds: int | None = None  # a new polling interval, with SLOW_DOWN


class PendingLogin(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: UUID
    member_id: UUID
    chat: ChatRef  # the private chat the login was started from or sent to
    claim_key: str
    claim_token: str  # "<random id>:<unix time sent>"; the check stops if it is no longer current
    device_code: SecretStr
    expires_at: AwareDatetime
    interval_seconds: int


class CodeHostLogin(Protocol):
    provider: str

    async def start(self) -> DeviceLogin: ...

    async def check(self, device_code: SecretStr) -> LoginCheck:
        """Approved checks carry the account; the code host's token is used once, then dropped."""
        ...


class LoginJobs(Protocol):
    """Schedules checks and keeps one live login per person. A claim holds the current login's
    token; a check whose token is no longer the claim's stops quietly (it was replaced)."""

    async def check_login_later(self, pending: PendingLogin, delay_seconds: int) -> None: ...

    async def claim(self, key: str, token: str, seconds: int) -> bool:
        """False if another login holds the claim."""
        ...

    async def current(self, key: str) -> str | None: ...

    async def replace(self, key: str, token: str, seconds: int) -> None: ...

    async def release(self, key: str, token: str) -> None:
        """Only if the claim is still this login's."""
        ...

    async def hold(self, key: str, seconds: int) -> None:
        """Make the claim last exactly as long as the code it protects."""
        ...


class IdentityLinking:
    def __init__(
        self,
        *,
        login: CodeHostLogin,
        workspaces: Workspaces,
        members: Members,
        audit: AuditLog,
        jobs: LoginJobs,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._login = login
        self._workspaces = workspaces
        self._members = members
        self._audit = audit
        self._jobs = jobs
        self._now = now

    async def start(
        self, message: InboundMessage, messaging: MessagingAdapter, *, switch: bool = False
    ) -> None:
        """`switch`: link another account even if one is already linked (it replaces it)."""
        if message.sender.is_anonymous:
            await _reply(messaging, message, messages.IAM_ANONYMOUS)
            return
        if not switch and (login := await self._linked_login(message)):
            await _reply(messaging, message, messages.IAM_ALREADY_LINKED.format(login=login))
            return
        key = f"{message.sender.platform}:{message.sender.user_id}"
        now = self._now()
        token = f"{uuid4().hex}:{int(now.timestamp())}"
        if not await self._jobs.claim(key, token, LOGIN_CLAIM_SECONDS):
            if not _sent_long_ago(await self._jobs.current(key), now):
                await _reply(messaging, message, messages.IAM_JUST_SENT)
                return
            await self._jobs.replace(key, token, LOGIN_CLAIM_SECONDS)  # the old code is dropped
        try:
            pending = await self._send_code(message, messaging, key, token)
            if pending is not None:
                await self._jobs.check_login_later(pending, pending.interval_seconds)
                return
        except BaseException:
            await self._jobs.release(key, token)
            raise
        await self._jobs.release(key, token)

    async def _linked_login(self, message: InboundMessage) -> str | None:
        workspace = await self._workspaces.for_chat(message.chat)
        if workspace is None:
            return None
        member = await self._members.for_chat_user(workspace.id, message.sender)
        return member.codehost.login if member.codehost else None

    async def _send_code(
        self, message: InboundMessage, messaging: MessagingAdapter, key: str, token: str
    ) -> PendingLogin | None:
        """None when the code couldn't be delivered privately (the person was told why)."""
        try:
            device = await self._login.start()
        except (
            CodeHostError
        ):  # e.g. device flow disabled in the App: tell the person, log the cause
            await _reply(messaging, message, messages.GITHUB_LOGIN_UNAVAILABLE)
            raise
        await self._jobs.hold(key, device.expires_in_seconds)
        private = messaging.private_chat(message.sender)
        text = messages.IAM_CODE.format(
            name=message.sender.display_name,
            url=device.verification_url,
            code=device.user_code,
            minutes=max(1, math.ceil(device.expires_in_seconds / 60)),
        )
        try:
            await messaging.send_message(private, text)
        except MessagingForbiddenError:  # the person never opened a private chat with the bot
            link = messaging.private_chat_link()
            await _reply(messaging, message, messages.IAM_OPEN_PRIVATE_CHAT.format(link=link))
            return None
        if not message.is_private_chat:
            await _reply(messaging, message, messages.IAM_SENT_PRIVATELY)
        # Rows are created only once a real person has a code in hand.
        workspace = await self._workspaces.get_or_create_for_chat(
            message.chat, name=f"{message.chat.platform} chat {message.chat.chat_id}"
        )
        member = await self._members.for_chat_user(workspace.id, message.sender)
        return PendingLogin(
            workspace_id=workspace.id,
            member_id=member.id,
            chat=private,
            claim_key=key,
            claim_token=token,
            device_code=device.device_code,
            expires_at=self._now() + timedelta(seconds=device.expires_in_seconds),
            interval_seconds=device.interval_seconds,
        )

    async def check(self, pending: PendingLogin, messaging: MessagingAdapter) -> None:
        if await self._jobs.current(pending.claim_key) != pending.claim_token:
            return  # replaced by a newer code (or already finished): stop quietly
        if self._now() >= pending.expires_at:
            await self._finish(pending, messaging, messages.IAM_EXPIRED)
            return
        try:
            result = await self._login.check(pending.device_code)
        except CodeHostError:  # a hiccup: try again at the next interval
            await self._jobs.check_login_later(pending, pending.interval_seconds)
            return
        match result.status:
            case LoginStatus.PENDING:
                await self._jobs.check_login_later(pending, pending.interval_seconds)
            case LoginStatus.SLOW_DOWN:
                interval = result.interval_seconds or (
                    pending.interval_seconds + SLOW_DOWN_EXTRA_SECONDS
                )
                slower = pending.model_copy(update={"interval_seconds": interval})
                await self._jobs.check_login_later(slower, interval)
            case LoginStatus.APPROVED if result.account is not None:
                await self._link(pending, result.account, messaging)
            case LoginStatus.DENIED:
                await self._finish(pending, messaging, messages.IAM_DENIED)
            case _:
                await self._finish(pending, messaging, messages.IAM_EXPIRED)

    async def _finish(self, pending: PendingLogin, messaging: MessagingAdapter, text: str) -> None:
        await self._jobs.release(pending.claim_key, pending.claim_token)
        await messaging.send_message(pending.chat, text)

    async def _link(
        self, pending: PendingLogin, account: CodeHostAccount, messaging: MessagingAdapter
    ) -> None:
        await self._jobs.release(pending.claim_key, pending.claim_token)
        member = await self._members.link_codehost(pending.workspace_id, pending.member_id, account)
        if member is None:  # removed from the workspace meanwhile
            return
        await self._audit.record(
            AuditEntry(
                workspace_id=pending.workspace_id,
                actor_member_id=member.id,
                action="verify_identity",
                result=AuditResult.EXECUTED,
                payload={
                    "provider": account.provider,
                    "user_id": account.user_id,
                    "login": account.login,
                },
            )
        )
        await messaging.send_message(pending.chat, messages.IAM_DONE.format(login=account.login))


def _sent_long_ago(token: str | None, now: datetime) -> bool:
    if token is None:  # the previous login just ended
        return True
    _, _, sent = token.rpartition(":")
    if not sent.isdigit():  # a claim from an older chirpo version: let it be replaced
        return True
    return now.timestamp() - int(sent) >= NEW_CODE_AFTER_SECONDS


async def _reply(messaging: MessagingAdapter, message: InboundMessage, text: str) -> None:
    await messaging.send_message(message.chat, text, reply_to=message.message_id)
