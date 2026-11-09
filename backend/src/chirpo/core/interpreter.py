"""A plain-language request, from message to the dry-run reply (D47).

Order: cheap checks first (linked repo, model, a verified sender) so no model call is wasted; then
the sender's read access to the repo, before anything of it is read, sent to the model or quoted;
then context, the model's proposals, resolution against the workspace (which checks read access
to any other repo named), and the sender's permission for every step. Nothing runs on the code
host here: the reply says what would happen.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress

from chirpo.core import messages
from chirpo.core.actions import permission_of
from chirpo.core.commands.base import Actor, Request, allowed, audit, find_actor, needs_services
from chirpo.core.context import MAX_ISSUES, Block, Team, context_blocks, default_of
from chirpo.core.domain import AuditResult, IssueQuery, RecentMessage, RepoRef
from chirpo.core.dryrun import describe
from chirpo.core.errors import (
    CodeHostError,
    LLMError,
    LLMInvalidOutputError,
    LLMRateLimitedError,
    LLMRefusedError,
    MessagingError,
)
from chirpo.core.listing import is_read, run_read
from chirpo.core.llm import Interpretation, interpret_request
from chirpo.core.permissions import Action
from chirpo.core.ports import LLMProvider
from chirpo.core.resolution import Question, resolve
from chirpo.core.workspaces import LinkedRepo

RECENT_MESSAGES = 20
TYPING_REFRESH_SECONDS = 4.0  # Telegram shows "typing…" for 5 seconds per request


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

    default = default_of(repos)
    # Nothing of a repo is read, sent to the model or quoted back before this check.
    if not await allowed(request, actor, Action.VIEW, default):
        return
    async with _typing(request):
        await _answer(request, actor, services.llm, repos)


async def _answer(
    request: Request, actor: Actor, llm: LLMProvider, repos: Sequence[LinkedRepo]
) -> None:
    services, default = actor.services, default_of(repos)
    try:
        open_issues = await services.codehost.list_issues(default, IssueQuery(limit=MAX_ISSUES))
    except CodeHostError:
        await request.reply(messages.GITHUB_TROUBLE)
        raise
    team = Team(
        members=await services.members.all(actor.workspace.id),
        repos=repos,
        open_issues=open_issues,
    )
    recent, replied = await _conversation(request, actor)
    interpretation = await _ask_model(
        request,
        llm,
        request.message.text,
        context=context_blocks(team, recent=recent, replied=replied),
    )
    if interpretation is None:
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
    if result is None:  # a repo the sender may not read; they were told
        return
    if isinstance(result, Question):
        await request.reply(result.text)
        return
    if not result:
        await request.reply(messages.NOT_A_REQUEST.format(bot=request.messaging.bot_mention()))
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
    if changes:
        parts.append(describe(changes, default))
    await request.reply("\n\n".join(parts))


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
    request: Request, llm: LLMProvider, text: str, *, context: list[Block]
) -> Interpretation | None:
    """The model's proposals, or None after telling the person why there are none."""
    sender = request.message.sender.display_name
    try:
        return await llm.interpret(interpret_request(text, sender, context=context))
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
