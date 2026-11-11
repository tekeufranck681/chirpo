"""Cards and button presses (M5.2, D53).

A change is stored as a pending action and shown as a card with Confirm and Cancel. A press
arrives as untrusted data: only an id in it is used, and everything else is read from the stored row
and checked in this order: the row is in this workspace, the press is on the bot's own card in the
same chat, the person may press, and only then the row is moved on atomically, so that of two
simultaneous presses exactly one runs it.
"""

import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress
from typing import Any
from uuid import UUID

from chirpo.core import messages
from chirpo.core.commands.base import Request, Services
from chirpo.core.context import MAX_NAME, clean
from chirpo.core.domain import AuditResult, Button, InboundCallback, RepoRef, SentMessage
from chirpo.core.dryrun import MAX_TEXT, describe
from chirpo.core.errors import MessagingError
from chirpo.core.executor import Outcome, execute
from chirpo.core.pending import PendingAction, PendingStatus, Step
from chirpo.core.permissions import Role
from chirpo.core.ports import MessagingAdapter
from chirpo.core.resolution import Resolved
from chirpo.core.workspaces import AuditEntry, Member

MAX_CARD = 4000  # Telegram refuses messages over 4096 characters
SHORT_TEXT = 120
CONFIRM = "ok"
CANCEL = "no"

log = logging.getLogger(__name__)


async def propose(
    request: Request,
    asker: Member,
    services: Services,
    steps: Sequence[Resolved],
    default_repo: RepoRef,
    notes: Sequence[str] = (),
) -> None:
    """Stores the steps and posts the card, as a reply to the request."""
    message = request.message
    name = clean(asker.display_name, MAX_NAME)
    footer = [messages.CONFIRM_FOOTER.format(name=name), *notes]
    for limit in (MAX_TEXT, SHORT_TEXT):  # long bodies are shortened before anything is dropped
        text = "\n\n".join([describe(steps, default_repo, limit), *footer])
        if len(text) <= MAX_CARD:
            break
    else:  # the person must see every step they confirm: too many to show means too many to ask
        await request.reply(messages.TOO_BIG)
        return
    pending = await services.pending.create(
        workspace_id=asker.workspace_id,
        requested_by=asker.id,
        chat=message.chat,
        steps=[Step.of(step.proposal, step.repo) for step in steps],
        expires_at=services.now() + services.proposal_ttl,
    )
    sent = await request.messaging.send_message(
        message.chat,
        text,
        reply_to=message.message_id,
        buttons=[
            Button(text=messages.BUTTON_CONFIRM, data=f"{CONFIRM}:{pending.id}"),
            Button(text=messages.BUTTON_CANCEL, data=f"{CANCEL}:{pending.id}"),
        ],
    )
    await services.pending.attach_message(asker.workspace_id, pending.id, sent.message_id)


async def handle_callback(
    event: InboundCallback, messaging: MessagingAdapter, services: Services | None
) -> None:
    if event.sender.is_bot:
        return
    press = _Press(event, messaging)
    parsed = _parse(event.data)
    if services is None or parsed is None:
        await press.answer()  # not one of our buttons: stop the spinner, say nothing
        return
    kind, pending_id = parsed
    if event.sender.is_anonymous:  # posted "as the group": no one in particular
        await press.answer(messages.CB_ANONYMOUS)
        return
    workspace = await services.workspaces.for_chat(event.chat)
    row = await services.pending.get(workspace.id, pending_id) if workspace else None
    if workspace is None or row is None or not _is_its_card(row, event):
        await press.answer(messages.CB_UNAVAILABLE)
        return
    if row.status != PendingStatus.PROPOSED:
        await press.answer(_note_for(row.status))
        return
    members = await services.members.all(workspace.id)
    presser = await services.members.for_chat_user(workspace.id, event.sender)
    asker = next((m for m in members if m.id == row.requested_by), None)
    if asker is None:
        await press.answer(messages.CB_UNAVAILABLE)
        return
    if presser.id != asker.id and not presser.role.at_least(Role.MAINTAINER):
        name = clean(asker.display_name, MAX_NAME)
        await press.answer(messages.CB_NOT_ALLOWED.format(name=name))
        return
    card = SentMessage(chat=event.chat, message_id=event.message_id)  # checked: it is the row's
    run = _Handling(press, services, row, asker, presser, card)
    if row.is_expired(services.now()):
        await run.expire()
    elif kind == CANCEL:
        await run.cancel()
    else:
        await run.confirm()


class _Press:
    """Talks back about one button press."""

    def __init__(self, event: InboundCallback, messaging: MessagingAdapter) -> None:
        self.event = event
        self.messaging = messaging

    async def answer(self, note: str | None = None) -> None:
        with suppress(MessagingError):  # the platform drops old presses; nothing to do about it
            await self.messaging.answer_callback(self.event, note)

    async def show(self, card: SentMessage, text: str, *, resend: bool = False) -> None:
        """Replaces the card's text (and removes its buttons). With `resend`, a card that can't
        be edited any more gets the text as a reply instead: a result must not be lost."""
        try:
            await self.messaging.edit_message(card, text)
        except MessagingError:
            if resend:
                with suppress(MessagingError):
                    await self.messaging.send_message(card.chat, text, reply_to=card.message_id)


class _Handling:
    def __init__(
        self,
        press: _Press,
        services: Services,
        row: PendingAction,
        asker: Member,
        presser: Member,
        card: SentMessage,
    ) -> None:
        self.press, self.services, self.row = press, services, row
        self.asker, self.presser, self.card = asker, presser, card

    async def cancel(self) -> None:
        if not await self._advance(PendingStatus.PROPOSED, PendingStatus.REJECTED):
            await self._lost()
            return
        await self.press.answer(messages.CB_CANCELLED)
        name = clean(self.presser.display_name, MAX_NAME)
        await self.press.show(self.card, messages.CANCELLED.format(name=name))
        await self._audit(AuditResult.REJECTED)

    async def expire(self) -> None:
        await self._advance(PendingStatus.PROPOSED, PendingStatus.EXPIRED)
        await self.press.answer(messages.CB_EXPIRED)
        await self.press.show(self.card, messages.EXPIRED)

    async def confirm(self) -> None:
        if not await self._advance(PendingStatus.PROPOSED, PendingStatus.CONFIRMED):
            await self._lost()
            return
        await self.press.answer(messages.CB_STARTED)  # before the slow part: answers go stale
        try:
            await self.press.show(self.card, messages.RUNNING)
            await self._advance(PendingStatus.CONFIRMED, PendingStatus.EXECUTING)
            outcome = await execute(
                self.row.steps,
                requester=self.asker,
                confirmer=self.presser,
                services=self.services,
            )
            status = PendingStatus.FAILED if outcome.problem else PendingStatus.EXECUTED
            result = {"lines": outcome.done, "problem": outcome.problem}
            await self._advance(PendingStatus.EXECUTING, status, result=result)
        except BaseException:  # also a cancelled job: a row must never stay "running"
            await asyncio.shield(self._crashed())
            raise
        await self.press.show(self.card, _report(outcome, len(self.row.steps)), resend=True)

    async def _lost(self) -> None:
        """The move didn't happen: someone else got the row first, or it ran out of time between
        the check and the move."""
        row = await self.services.pending.get(self.row.workspace_id, self.row.id)
        if row is not None and row.status == PendingStatus.PROPOSED:
            await self.expire()  # nobody took it, so it must have expired just now
            return
        await self.press.answer(_note_for(row.status) if row else messages.CB_UNAVAILABLE)

    async def _crashed(self) -> None:
        for expect in (PendingStatus.EXECUTING, PendingStatus.CONFIRMED):
            with suppress(Exception):  # best effort: the original error is what matters
                await self._advance(expect, PendingStatus.FAILED, result={"error": "crashed"})
        await self.press.show(self.card, messages.CRASHED, resend=True)

    async def _advance(
        self,
        expect: PendingStatus,
        to: PendingStatus,
        result: dict[str, Any] | None = None,
    ) -> bool:
        decided = to in (PendingStatus.CONFIRMED, PendingStatus.REJECTED)
        moved = await self.services.pending.advance(
            self.row.workspace_id,
            self.row.id,
            expect=expect,
            to=to,
            now=self.services.now(),
            by=self.presser.id if decided else None,  # who decided
            result=result,
        )
        return moved is not None

    async def _audit(self, result: AuditResult) -> None:
        await self.services.audit.record(
            AuditEntry(
                workspace_id=self.row.workspace_id,
                actor_member_id=self.asker.id,
                confirmed_by_member_id=self.presser.id,
                action=self.row.steps[0].action,
                result=result,
                payload={"steps": [step.action for step in self.row.steps]},
            )
        )


def _note_for(status: PendingStatus) -> str:
    """What to tell someone who pressed a button whose request isn't waiting for an answer."""
    match status:
        case PendingStatus.EXPIRED:
            return messages.CB_EXPIRED
        case PendingStatus.CONFIRMED | PendingStatus.EXECUTING:
            return messages.CB_RUNNING
        case _:
            return messages.CB_ALREADY


def _parse(data: str) -> tuple[str, UUID] | None:
    kind, _, raw = data.partition(":")
    if kind not in (CONFIRM, CANCEL):
        return None
    try:
        return kind, UUID(raw)
    except ValueError:
        return None


def _is_its_card(row: PendingAction, event: InboundCallback) -> bool:
    """The button must be on the card the bot posted for this row, in the row's own chat."""
    return row.message_id == event.message_id and row.chat == event.chat


def _report(outcome: Outcome, steps: int) -> str:
    lines = list(outcome.done)
    if outcome.problem is not None:
        lines.append(messages.STOPPED.format(reason=outcome.problem))
        if outcome.stopped_at is not None and outcome.stopped_at < steps - 1:
            lines.append(messages.STOPPED_REST)
    text = "\n".join(lines)
    return text if len(text) <= MAX_CARD else text[: MAX_CARD - 1] + "…"
