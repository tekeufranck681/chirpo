"""What did I miss? (M6.9, D22): the kept messages since the asker's last one, summed up by the
model, and the issues opened and closed since then, read from GitHub. Answered in the group, so
the team can see and correct it. Nothing is acted on.
"""

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from chirpo.core import messages
from chirpo.core.commands.base import Actor, Request, audit
from chirpo.core.context import chat_line
from chirpo.core.domain import AuditResult, RecentMessage, RepoRef
from chirpo.core.errors import CodeHostError, LLMError, LLMRateLimitedError, LLMRefusedError
from chirpo.core.llm import catch_up_request
from chirpo.core.outbound import plain_lines
from chirpo.core.permissions import Action
from chirpo.core.ports import LLMProvider
from chirpo.core.summary import changes_since, named

log = logging.getLogger(__name__)

MAX_MESSAGES = 200  # what one summary reads at most: the latest ones
MAX_SUMMARY_LINES = 8
MAX_SUMMARY_LINE = 300
MAX_CHARS = 30_000  # what one summary sends at most (~8k tokens): the newest lines


async def catch_up(
    request: Request,
    actor: Actor,
    llm: LLMProvider,
    repos: Sequence[RepoRef],
    *,
    hours: int | None = None,
) -> None:
    services, workspace = actor.services, actor.workspace
    buffer = services.buffer
    if buffer is None or not workspace.keeps_context:
        await request.reply(messages.CATCH_UP_NEEDS_CONTEXT)
        return
    now = services.now()
    kept = await buffer.recent(workspace.id, request.message.chat, limit=MAX_MESSAGES + 1)
    notes = []
    if hours is not None:
        hours = min(hours, services.context_hours)  # nothing older is kept
        since = now - timedelta(hours=hours)
        missed = [m for m in kept if m.sent_at >= since]
    else:
        asker = request.message.sender.user_id
        own = [i for i, m in enumerate(kept) if m.sender.user_id == asker]
        if own:
            since, missed = kept[own[-1]].sent_at, kept[own[-1] + 1 :]
        else:
            since = kept[0].sent_at if kept else now - timedelta(hours=services.context_hours)
            missed = kept
            if len(kept) <= MAX_MESSAGES:  # all of it is here, so there is truly none of theirs
                notes.append(messages.CATCH_UP_NO_OWN_MESSAGE.format(hours=services.context_hours))
    if len(missed) > MAX_MESSAGES:
        missed = missed[-MAX_MESSAGES:]
        notes.append(messages.CATCH_UP_CUT.format(count=MAX_MESSAGES))
    issues = await _issue_changes(actor, repos, since)
    when = _when(since, now, workspace.timezone)
    if not missed and not issues:
        await request.reply(messages.CATCH_UP_NOTHING.format(since=when))
        return
    if hours is not None:
        heading = messages.CATCH_UP_HEADING_HOURS.format(period=messages.duration(hours * 3600))
    else:
        heading = messages.CATCH_UP_HEADING.format(since=when)
    parts = [heading]
    if missed:
        lines = _newest_within(missed, ZoneInfo(workspace.timezone))
        if len(lines) < len(missed):
            notes.append(messages.CATCH_UP_CUT_CHARS)
        summary = await _summary(request, actor, llm, lines)
        if summary is None:
            return
        parts.append(summary)
    if issues:
        parts.append("\n".join(issues))
    await audit(actor, "catch_up", AuditResult.EXECUTED, {"messages": len(missed)})
    await request.reply("\n\n".join([*parts, *notes]))


def _newest_within(missed: Sequence[RecentMessage], zone: ZoneInfo) -> list[str]:
    """The messages as lines, oldest first, keeping the newest that fit in MAX_CHARS."""
    kept: list[str] = []
    size = 0
    for message in reversed(missed):
        line = chat_line(message, zone=zone)
        size += len(line) + 1
        if size > MAX_CHARS:
            break
        kept.append(line)
    return kept[::-1]


async def _summary(
    request: Request, actor: Actor, llm: LLMProvider, lines: Sequence[str]
) -> str | None:
    """The model's summary, cleaned like all model text; None after telling the person why
    there is none."""
    asked = catch_up_request(lines, request.message.sender.display_name)
    try:
        answer = await llm.summarize(asked)
    except LLMRateLimitedError:
        await request.reply(messages.LLM_BUSY)
        return None
    except LLMRefusedError:
        await request.reply(messages.CATCH_UP_NO_SUMMARY)
        return None
    except LLMError:
        await request.reply(messages.LLM_UNAVAILABLE)
        raise  # setup problems and outages belong in the logs
    limits = actor.services.limits
    if limits is not None:
        await limits.add_tokens(actor.workspace.id, answer.input_tokens + answer.output_tokens)
    text = plain_lines(answer.text, line_limit=MAX_SUMMARY_LINE, max_lines=MAX_SUMMARY_LINES)
    if text is None:
        await request.reply(messages.CATCH_UP_NO_SUMMARY)
    return text


async def _issue_changes(actor: Actor, repos: Sequence[RepoRef], since: datetime) -> list[str]:
    """Lines for the linked repos the asker may read; a repo GitHub doesn't answer for is left
    out quietly, since the summary is the main answer."""
    lines = []
    for repo in repos:
        try:
            verdict = await actor.services.permissions.check(actor.member, Action.VIEW, repo)
            if not verdict.allowed:
                continue
            opened, closed, _ = await changes_since(repo, actor.services.codehost, since)
        except CodeHostError as error:
            log.warning("catch_up_repo_unavailable error=%s", type(error).__name__)
            continue
        if opened:
            lines.append(messages.CATCH_UP_OPENED.format(repo=repo.full_name, issues=named(opened)))
        if closed:
            lines.append(messages.CATCH_UP_CLOSED.format(repo=repo.full_name, issues=named(closed)))
    return lines


def _when(moment: datetime, now: datetime, timezone: str) -> str:
    zone = ZoneInfo(timezone)
    local, today = moment.astimezone(zone), now.astimezone(zone).date()
    time = f"{local:%H:%M}" + (" UTC" if timezone == "UTC" else "")  # no timezone set yet
    if local.date() == today:
        return messages.TODAY_AT.format(time=time)
    day = (
        f"{messages.WEEKDAY_NAMES[local.weekday()]} {local.day} {messages.MONTHS[local.month - 1]}"
    )
    return messages.DAY_AT.format(day=day, time=time)
