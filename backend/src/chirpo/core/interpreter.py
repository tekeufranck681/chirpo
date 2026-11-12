"""A plain-language request, from message to the answer or the confirmation card (D47, D53).

Order: cheap checks first (linked repo, model, a verified sender) so no model call is wasted; then
the sender's read access to the repo, before anything of it is read, sent to the model or quoted;
then context, the model's proposals, resolution against the workspace (which checks read access
to any other repo named), and the sender's permission for every step. Reads are answered here.
Changes are stored and shown as a card to confirm: nothing is changed on the code host until a
person presses Confirm (core/confirmation.py).
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

from chirpo.core import messages
from chirpo.core.actions import permission_of, spec_of
from chirpo.core.commands.base import (
    Actor,
    Request,
    Services,
    allowed,
    audit,
    find_actor,
    limited,
    needs_services,
)
from chirpo.core.confirmation import may_decide, propose
from chirpo.core.context import (
    MAX_ISSUES,
    MAX_PROPOSAL_CHARS,
    Block,
    Team,
    context_blocks,
    default_of,
    proposal_size,
)
from chirpo.core.direct import run_now, runs_without_card
from chirpo.core.domain import Attachment, AuditResult, IssueQuery, RecentMessage, RepoRef
from chirpo.core.errors import (
    CodeHostError,
    LLMError,
    LLMInvalidOutputError,
    LLMRateLimitedError,
    LLMRefusedError,
    MessagingError,
)
from chirpo.core.listing import is_read, run_read
from chirpo.core.llm import Interpretation, RequestImage
from chirpo.core.lookback import Asked, Charge, recent_image
from chirpo.core.lookback import interpret as ask_with_lookback
from chirpo.core.pending import PendingAction, PendingStatus
from chirpo.core.permissions import Action
from chirpo.core.ports import LLMProvider
from chirpo.core.resolution import Question, Resolved, resolve
from chirpo.core.vision import Seen, gather_images, gather_recent
from chirpo.core.workspaces import LinkedRepo

RECENT_MESSAGES = 20
TYPING_REFRESH_SECONDS = 4.0  # Telegram shows "typing…" for 5 seconds per request

log = logging.getLogger(__name__)


async def interpret(request: Request) -> None:
    services = await needs_services(request)
    if services is None:
        return
    if services.llm is None:
        await request.reply(messages.LLM_NOT_CONFIGURED)
        return
    actor = await find_actor(request, no_workspace=messages.NO_REPO_LINKED)
    if actor is None:
        return
    repos = await services.workspaces.repos(actor.workspace.id)
    if not repos:
        await request.reply(messages.NO_REPO_LINKED)
        return
    if not actor.member.is_verified:
        await request.reply(messages.LINK_GITHUB_FIRST)
        return

    card, answered = await _card_replied_to(request, actor)  # a reply to a card: no model needed
    if answered:
        return
    if services.limits is not None:  # before anything is read or paid for
        verdict = await services.limits.check_llm(actor.workspace.id, actor.member.id)
        if await limited(request, verdict):
            return
    default = default_of(repos)
    # Nothing of a repo is read, sent to the model or quoted back before this check.
    if not await allowed(request, actor, Action.VIEW, default):
        return
    async with _typing(request):
        await _answer(request, actor, services.llm, repos, card)


class _Timer:
    """Milliseconds per step of a request, logged once at the end: no message content."""

    def __init__(self) -> None:
        self._start = self._last = time.perf_counter()
        self._laps: dict[str, int] = {}

    def lap(self, step: str) -> None:
        now = time.perf_counter()
        self._laps[step] = round((now - self._last) * 1000)
        self._last = now

    def log(self) -> None:
        steps = " ".join(f"{step}_ms={ms}" for step, ms in self._laps.items())
        total = round((time.perf_counter() - self._start) * 1000)
        log.info("request_interpreted %s total_ms=%d", steps, total)


CREATES = frozenset({"create_issue", "create_issues"})


def _loses_the_new_issues(card: PendingAction, changes: Sequence[Resolved]) -> bool:
    """A card that creates issues, answered by steps that create none: the model took "it" for
    an issue already on GitHub (the card's issues aren't there yet), so the answer would drop
    what the card was for. The card is left as it is."""
    if not any(step.action in CREATES for step in card.steps):
        return False
    return not any(spec_of(step.proposal).name in CREATES for step in changes)


async def _card_replied_to(request: Request, actor: Actor) -> tuple[PendingAction | None, bool]:
    """The waiting card this message replies to, if the sender may change it (M5.3); and whether
    the reply was already answered. A card that was handled or ran out of time is just context
    the sender replied to: what they write is an ordinary request, never swallowed."""
    message, services = request.message, actor.services
    if not (message.replies_to_bot and message.reply_to_message_id):
        return None, False
    row = await services.pending.find_by_card(
        actor.workspace.id, message.chat, message.reply_to_message_id
    )
    if row is None or not may_decide(actor.member, row):
        return None, False  # not one of our cards, or not theirs to change
    if row.status != PendingStatus.PROPOSED or row.is_expired(services.now()):
        return None, False
    if proposal_size(row.steps) > MAX_PROPOSAL_CHARS:
        await request.reply(messages.TOO_BIG_TO_CHANGE)  # never show the model half a proposal
        return None, True
    return row, False


async def _answer(
    request: Request,
    actor: Actor,
    llm: LLMProvider,
    repos: Sequence[LinkedRepo],
    replacing: PendingAction | None,
) -> None:
    timer = _Timer()
    try:
        await _answer_timed(request, actor, llm, repos, timer, replacing)
    except BaseException:
        timer.lap("failed")  # the step that was running
        raise
    finally:
        timer.log()


async def _answer_timed(
    request: Request,
    actor: Actor,
    llm: LLMProvider,
    repos: Sequence[LinkedRepo],
    timer: _Timer,
    replacing: PendingAction | None,
) -> None:
    services, default = actor.services, default_of(repos)
    try:  # at the same time; the first failure cancels the others
        async with asyncio.TaskGroup() as group:
            issues = group.create_task(
                services.codehost.list_issues(default, IssueQuery(limit=MAX_ISSUES))
            )
            members = group.create_task(services.members.all(actor.workspace.id))
            conversation = group.create_task(_conversation(request, actor))
            seen_task = group.create_task(_seen(request, services))
    except ExceptionGroup as failures:
        error = failures.exceptions[0]
        if isinstance(error, CodeHostError):
            await request.reply(messages.GITHUB_TROUBLE)
        raise error from None
    open_issues, (recent, replied) = issues.result(), conversation.result()
    seen = seen_task.result()
    team = Team(members=members.result(), repos=repos, open_issues=open_issues)
    timer.lap("context")
    # An image nobody attached is only looked at if the model asks (D52).
    look = None
    if services.read_images and not seen.images:
        look = recent_image(recent, now=datetime.now(UTC))
    asked = await _ask_model(
        request,
        llm,
        request.message.text,
        context=context_blocks(
            team,
            recent=recent,
            replied=replied,
            media=services.read_images,
            look_at=look.message_id if look else None,
            proposal=replacing.steps if replacing else None,
        ),
        images=seen.images,
        candidate=look.attachment if look else None,
        charge=_charger(services, actor),
    )
    notes = seen.notes
    del seen  # the image bytes are done with
    timer.lap("model")
    if asked is None:
        return
    interpretation = asked.interpretation
    notes = [*notes, *asked.notes]
    if asked.failed:  # the model wanted an image that couldn't be fetched
        await request.reply("\n".join(notes) or messages.IMAGE_UNAVAILABLE)
        return

    async def can_read(repo: RepoRef) -> bool:
        return await allowed(request, actor, Action.VIEW, repo)

    try:
        result = await resolve(
            interpretation.proposals,
            team=team,
            sender=actor.member,
            codehost=services.codehost,
            can_read=can_read,
        )
    except CodeHostError:
        await request.reply(messages.GITHUB_TROUBLE)
        raise
    timer.lap("resolve")
    if result is None:  # a repo the sender may not read; they were told
        return
    if isinstance(result, Question):
        await request.reply(_with_notes(result.text, notes))
        return
    if not result:
        nothing = messages.NOT_A_REQUEST.format(bot=request.messaging.bot_mention())
        if replacing is not None:  # "ok, looks good": only the buttons confirm
            nothing = messages.USE_THE_BUTTONS
        await request.reply(_with_notes(nothing, notes))
        return
    for step in result:
        action = permission_of(step.proposal)
        if action is not None and not await allowed(request, actor, action, step.repo):
            return
    parts = []
    for step in filter(is_read, result):  # they change nothing: answered for real
        try:
            parts.append(await run_read(step, services.codehost))
        except CodeHostError:
            await request.reply(messages.GITHUB_TROUBLE)
            raise
        await audit(actor, Action.VIEW, AuditResult.EXECUTED, {"repo": step.repo.full_name})
    changes = [step for step in result if not is_read(step)]
    if parts:
        await request.reply(
            "\n\n".join(parts) if changes else _with_notes("\n\n".join(parts), notes)
        )
    if changes and replacing is not None and _loses_the_new_issues(replacing, changes):
        await request.reply(messages.CARD_UNCHANGED)  # the card stays, to confirm or cancel
        return
    if changes:
        fresh = services.now() - request.message.sent_at <= services.stale_after
        own, quoted = request.message.text, replied.text if replied else ""
        if replacing is None and fresh and runs_without_card(changes, own, quoted):
            await run_now(request, actor.member, services, changes, notes)
            timer.lap("reply")
            return
        if not fresh:
            notes = [*notes, messages.STALE_NOTE]  # the worker was away: don't act on old words
        await propose(request, actor.member, services, changes, default, notes, replacing)
    timer.lap("reply")


@asynccontextmanager
async def _typing(request: Request) -> AsyncIterator[None]:
    """Keeps "typing…" on screen until the reply is sent: chat apps drop it after ~5 seconds,
    while the model can take 10-20, and a silent bot looks broken."""
    messaging, chat = request.messaging, request.message.chat

    async def show() -> None:
        with suppress(MessagingError):  # only a courtesy
            await messaging.show_typing(chat)

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


def _charger(services: Services, actor: Actor) -> Charge:
    async def charge(answer: Interpretation) -> None:
        if services.limits is not None:
            tokens = answer.input_tokens + answer.output_tokens
            await services.limits.add_tokens(actor.workspace.id, tokens)

    return charge


async def _seen(request: Request, services: Services) -> Seen:
    if not services.read_images:
        return Seen((), [])
    return await gather_images(request.messaging, request.message)


def _with_notes(text: str, notes: list[str]) -> str:
    return "\n\n".join([text, "\n".join(notes)]) if notes else text


async def _conversation(
    request: Request, actor: Actor
) -> tuple[list[RecentMessage], RecentMessage | None]:
    """The chat's recent messages and the one replied to, when the team keeps context (D46)."""
    buffer = actor.services.buffer
    if buffer is None or not actor.workspace.keeps_context:
        return [], None
    chat, workspace_id = request.message.chat, actor.workspace.id
    recent = await buffer.recent(workspace_id, chat, limit=RECENT_MESSAGES)
    replied = None
    if reply_to := request.message.reply_to_message_id:
        replied = await buffer.find(workspace_id, chat, reply_to)
        recent = [message for message in recent if message.message_id != reply_to]
    return recent, replied


async def _ask_model(
    request: Request,
    llm: LLMProvider,
    text: str,
    *,
    context: list[Block],
    images: Sequence[RequestImage],
    candidate: Attachment | None,
    charge: Charge,
) -> Asked | None:
    """The model's answer, or None after telling the person why there is none."""
    sender = request.message.sender.display_name

    async def fetch(attachment: Attachment) -> Seen:
        return await gather_recent(request.messaging, attachment)

    try:
        return await ask_with_lookback(
            llm,
            text=text,
            sender=sender,
            context=context,
            images=images,
            candidate=candidate,
            fetch=fetch,
            charge=charge,
        )
    except LLMRateLimitedError:
        await request.reply(messages.LLM_BUSY)
    except LLMRefusedError:
        await request.reply(messages.LLM_REFUSED)
    except LLMInvalidOutputError:
        await request.reply(messages.LLM_UNCLEAR)
    except LLMError:
        await request.reply(messages.LLM_UNAVAILABLE)
        raise  # setup problems and outages belong in the logs
    return None
