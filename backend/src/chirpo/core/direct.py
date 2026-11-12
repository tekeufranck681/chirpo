"""Changes that run without a card (M5.6, D56).

Status and label changes are cheap to reverse, so they don't ask first, but only when it's clear
they hit the issue the sender meant: the sender's own message (or the one they replied to) must
name it. An issue the model picked from context alone, such as an issue title or a recent message
that someone else wrote, always gets a card. Everything runs as the sender, is audited, and comes
back with an Undo button that reverses exactly what changed (core/confirmation.py).
"""

import asyncio
import logging
import re
from collections.abc import Sequence
from contextlib import suppress
from typing import Any

from chirpo.core import messages
from chirpo.core.actions import IssueRef, spec_of
from chirpo.core.commands.base import Request, Services
from chirpo.core.confirmation import report
from chirpo.core.domain import Button
from chirpo.core.errors import MessagingError
from chirpo.core.executor import execute
from chirpo.core.pending import PendingAction, PendingStatus, Step
from chirpo.core.permissions import Action
from chirpo.core.resolution import Resolved, significant_words
from chirpo.core.workspaces import Member

log = logging.getLogger(__name__)

# "#12", "issue 12", "ticket #12": the ways of naming an issue by number. Not "bug 12 times" or
# "task 2 of 5", and not a number with a word or a decimal glued to it ("#12th", "#12.5").
_REFERENCE = re.compile(r"(?:#|\b(?:issue|ticket)\s+#?)(\d+)(?!\w)(?!\.\d)")


def _numbers(text: str) -> set[int]:
    return {int(number) for number in _REFERENCE.findall(text.casefold())}


def named_in(ref: IssueRef, own: str, replied: str = "") -> bool:
    """Whether the issue is named by the sender, not only guessed from context.

    `own` is the sender's message. A number counts from it, or from the message they replied to
    only when that names exactly this one issue ("it" is then clear; with several it isn't). A
    description counts only from the sender's own words: every meaningful word must be there."""
    if ref.number is not None:
        return ref.number in _numbers(own) or _numbers(replied) == {ref.number}
    if ref.description is None:
        return False
    wanted = significant_words(ref.description)
    return bool(wanted) and wanted <= set(re.findall(r"[\w']+", own.casefold()))


def runs_without_card(steps: Sequence[Resolved], own: str, replied: str = "") -> bool:
    """True when every step is a status or label change on an issue the sender named. One step
    that asks first puts the whole request on one card: the person decides once."""
    return bool(steps) and all(_runs_without_card(step, own, replied) for step in steps)


def _runs_without_card(step: Resolved, own: str, replied: str) -> bool:
    spec = spec_of(step.proposal)
    if spec.needs_confirmation or spec.permission in (None, Action.VIEW):
        return False
    return step.ref is not None and named_in(step.ref, own, replied)


async def run_now(
    request: Request,
    asker: Member,
    services: Services,
    steps: Sequence[Resolved],
    notes: Sequence[str] = (),
) -> None:
    """Runs the steps for the asker and answers once, with Undo when something changed. The row
    is kept (`expires_at` is the end of the Undo window) so the button has something to reverse.
    The person is always told how it went, even when bookkeeping fails."""
    message = request.message
    pending = await services.pending.create(
        workspace_id=asker.workspace_id,
        requested_by=asker.id,
        chat=message.chat,
        steps=[Step.of(step.proposal, step.repo) for step in steps],
        expires_at=services.now() + services.proposal_ttl,
    )
    started = await _move(services, pending, PendingStatus.PROPOSED, PendingStatus.CONFIRMED)
    started = started and await _move(
        services, pending, PendingStatus.CONFIRMED, PendingStatus.EXECUTING
    )
    if not started:  # e.g. the row was swept or the store hiccuped: nothing was changed
        log.warning("direct_run_not_started")
        await request.reply(messages.NOT_RUN)
        return
    try:
        outcome = await execute(
            pending.steps,
            requester=asker,
            confirmer=None,
            services=services,
            pending_id=pending.id,
        )
    except BaseException:  # a cancelled job too: the row must not stay "executing"
        with suppress(Exception):
            crashed = {"error": "crashed"}
            await asyncio.shield(
                _move(services, pending, PendingStatus.EXECUTING, PendingStatus.FAILED, crashed)
            )
        with suppress(MessagingError):
            await request.reply(messages.CRASHED)
        raise
    status = PendingStatus.FAILED if outcome.problem else PendingStatus.EXECUTED
    result = {"lines": outcome.done, "problem": outcome.problem, "effects": outcome.effects}
    stored = await _try_move(services, pending, PendingStatus.EXECUTING, status, result)
    text = "\n\n".join([report(outcome, len(steps)), *notes])
    undo = Button(text=messages.BUTTON_UNDO, data=f"undo:{pending.id}")
    sent = await request.messaging.send_message(
        message.chat,
        text,
        reply_to=message.message_id,
        buttons=[undo] if outcome.effects and stored else [],  # no row, nothing to reverse from
    )
    await services.pending.attach_message(asker.workspace_id, pending.id, sent.message_id)


async def _move(
    services: Services,
    row: PendingAction,
    expect: PendingStatus,
    to: PendingStatus,
    result: dict[str, Any] | None = None,
) -> bool:
    moved = await services.pending.advance(
        row.workspace_id, row.id, expect=expect, to=to, now=services.now(), result=result
    )
    return moved is not None


async def _try_move(
    services: Services,
    row: PendingAction,
    expect: PendingStatus,
    to: PendingStatus,
    result: dict[str, Any],
) -> bool:
    """Records how the run ended; the change has happened either way, so a failure here is
    logged and not allowed to stop the reply."""
    try:
        stored = await _move(services, row, expect, to, result)
    except Exception as error:
        log.warning("direct_run_result_not_stored error=%s", type(error).__name__, exc_info=True)
        return False
    if not stored:
        log.warning("direct_run_result_lost status=%s", to)
    return stored
