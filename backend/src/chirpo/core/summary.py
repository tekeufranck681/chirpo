"""The daily summary (M6.5): one message a day per chat, at the time its admins chose, in the
workspace's timezone. Built from GitHub data and fixed text only: no LLM, so it costs nothing and
can't be steered by what an issue says. Quiet days still get one, saying nothing changed.
"""

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from functools import cache
from typing import TYPE_CHECKING, Any
from uuid import UUID
from zoneinfo import ZoneInfo, available_timezones

from chirpo.core import messages
from chirpo.core.domain import (
    AuditResult,
    Issue,
    IssueQuery,
    IssueState,
    Milestone,
    PullRequest,
    PullRequestQuery,
    RepoRef,
)
from chirpo.core.errors import CodeHostError, MessagingForbiddenError
from chirpo.core.outbound import shown
from chirpo.core.ports import CodeHostAdapter, MessagingAdapter
from chirpo.core.statuses import STATUSES, StatusLabels, shown_status
from chirpo.core.workspaces import AuditEntry, ScheduledSummary, SummarySchedule

if TYPE_CHECKING:  # commands import this module for /settings
    from chirpo.core.commands.base import Services

log = logging.getLogger(__name__)

WINDOW = timedelta(hours=2)  # a summary not posted by then (worker down) waits for tomorrow
DAY = timedelta(hours=24)
MAX_REPOS = 5
MAX_NEW = 5  # new issues named; the rest are counted
MAX_LISTED = 100  # issues read per list; beyond, the count shows "100+"
MAX_TITLE = 60
MAX_TEXT = 4000  # Telegram takes 4096 characters per message
SUMMARY_ACTION = "post_summary"  # in the audit log


@cache
def _zones() -> dict[str, str]:
    return {name.casefold(): name for name in available_timezones() | {"UTC"}}


def timezone_named(said: str) -> str | None:
    """The IANA name for what an admin typed ("africa/douala"), or None if there's no such zone.
    Only names from the system's own list are accepted."""
    return _zones().get(said.strip().casefold())


def parse_time(said: str) -> time | None:
    """ "9:00" or "09:30" (24-hour) as a time; None for anything else."""
    hours, colon, minutes = said.partition(":")
    if not colon or not hours.isdigit() or len(minutes) != 2 or not minutes.isdigit():
        return None
    if int(hours) > 23 or int(minutes) > 59:
        return None
    return time(int(hours), int(minutes))


def due_day(
    schedule: SummarySchedule, timezone: str, sent_on: date | None, now: datetime
) -> date | None:
    """The team's local day whose summary is due now, or None. Due from the chosen time until
    WINDOW later, once a day; with `weekdays_only`, never on Saturday or Sunday. Yesterday's slot
    counts too, so a 23:30 summary still has its full window after midnight."""
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    for day in (local.date(), local.date() - timedelta(days=1)):
        if schedule.weekdays_only and day.weekday() >= 5:
            continue
        start = datetime.combine(day, schedule.at, zone)
        if start <= local < start + WINDOW and sent_on != day:
            return day
    return None


def window_start(schedule: SummarySchedule, timezone: str, day: date) -> datetime:
    """When the period a summary covers starts: the previous slot, so nothing falls between two
    summaries (Friday's slot for a Monday one when weekends are skipped)."""
    previous = day - timedelta(days=1)
    while schedule.weekdays_only and previous.weekday() >= 5:
        previous -= timedelta(days=1)
    return datetime.combine(previous, schedule.at, ZoneInfo(timezone))


async def send_due(
    services: "Services", messengers: Mapping[str, MessagingAdapter], now: datetime
) -> int:
    """Posts every summary due now; returns how many were posted. One chat's trouble never
    stops the others. The day is claimed before posting, so it is never posted twice, and given
    back when posting fails for a reason that may pass, so the next minute tries again."""
    posted = 0
    for item in await services.workspaces.scheduled_summaries():
        day = due_day(item.schedule, item.workspace.timezone, item.sent_on, now)
        messenger = messengers.get(item.chat.platform)
        if day is None or messenger is None:
            continue
        try:
            posted += await _post(item, day, messenger, services, now)
        except Exception as error:  # one chat must not stop the rest
            log.warning("summary_failed error=%s", type(error).__name__)
    return posted


async def _post(
    item: ScheduledSummary,
    day: date,
    messenger: MessagingAdapter,
    services: "Services",
    now: datetime,
) -> int:
    workspace = item.workspace
    links = await services.workspaces.repos(workspace.id)
    if not links or not await services.workspaces.claim_summary(item.chat, day):
        return 0
    repos = [link.repo for link in links[:MAX_REPOS]]
    payload: dict[str, Any] = {"day": day.isoformat(), "repos": [r.full_name for r in repos]}
    try:
        since = window_start(item.schedule, workspace.timezone, day)
        text = await summary_text(
            repos, services.codehost, workspace.statuses, now, workspace.timezone, since=since
        )
        await messenger.send_message(item.chat, text)
    except MessagingForbiddenError:  # removed from the chat: trying every minute won't help
        await _audit(services, workspace.id, AuditResult.FAILED, payload, "chat_unreachable")
        return 0
    except Exception as error:
        await services.workspaces.release_summary(item.chat, day, item.sent_on)
        await _audit(services, workspace.id, AuditResult.FAILED, payload, type(error).__name__)
        raise
    await _audit(services, workspace.id, AuditResult.EXECUTED, payload)
    return 1


async def _audit(
    services: "Services",
    workspace_id: UUID,
    result: AuditResult,
    payload: dict[str, Any],
    error: str | None = None,
) -> None:
    entry = AuditEntry(
        workspace_id=workspace_id,
        actor_member_id=None,  # the schedule an admin set, not a person now
        action=SUMMARY_ACTION,
        result=result,
        payload=payload,
        error=error,
    )
    await services.audit.record(entry)


async def summary_text(
    repos: Sequence[RepoRef],
    codehost: CodeHostAdapter,
    statuses: StatusLabels,
    now: datetime,
    timezone: str,
    *,
    since: datetime | None = None,
) -> str:
    """One part per repo, in the order given; a repo GitHub doesn't answer for says so. `since`:
    when the period starts (24 hours ago by default). Cut to what one chat message holds."""
    local = now.astimezone(ZoneInfo(timezone))
    since = since or now - DAY
    day = f"{messages.WEEKDAYS[local.weekday()]} {local.day} {messages.MONTHS[local.month - 1]}"
    start = since.astimezone(ZoneInfo(timezone)).date()
    when = (
        messages.SUMMARY_YESTERDAY
        if start == local.date() - timedelta(days=1)
        else messages.WEEKDAY_NAMES[start.weekday()]
    )
    parts = []
    for repo in repos:
        try:
            parts.append(await _part(repo, codehost, statuses, since, day, when))
        except CodeHostError:
            log.warning("summary_repo_unavailable")
            parts.append(messages.SUMMARY_UNAVAILABLE.format(repo=repo.full_name))
    text = "\n\n".join(parts)
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


async def _part(
    repo: RepoRef,
    codehost: CodeHostAdapter,
    statuses: StatusLabels,
    since: datetime,
    day: str,
    when: str,
) -> str:
    recent, open_now, milestones, pulls = await asyncio.gather(
        codehost.list_issues(repo, IssueQuery(state="all", since=since, limit=MAX_LISTED)),
        codehost.list_issues(repo, IssueQuery(limit=MAX_LISTED)),
        codehost.list_milestones(repo),
        codehost.list_pull_requests(repo, PullRequestQuery(limit=MAX_LISTED)),
    )
    opened = sorted((i for i in recent if i.created_at >= since), key=lambda i: i.number)
    closed = [
        i for i in recent if i.state == IssueState.CLOSED and i.closed_at and i.closed_at >= since
    ]
    more = "+" if len(recent) >= MAX_LISTED else ""  # a very busy day: only the newest were read
    lines = [messages.SUMMARY_HEADING.format(repo=repo.full_name, day=day)]
    if opened or closed:
        lines.append(
            messages.SUMMARY_CHANGES.format(
                when=when, opened=f"{len(opened)}{more}", closed=f"{len(closed)}{more}"
            )
        )
        if opened:
            lines.append(messages.SUMMARY_NEW.format(issues=_named(opened)))
    else:
        lines.append(messages.SUMMARY_QUIET.format(when=when))
    capped = "+" if len(open_now) >= MAX_LISTED else ""
    lines.append(_open_line(open_now, statuses, capped))
    sprint = _sprint(milestones)
    if sprint is not None and sprint.due_on is not None:
        count = f"{sum(1 for i in open_now if i.milestone == sprint.number)}{capped}"
        due = f"{sprint.due_on.day} {messages.MONTHS[sprint.due_on.month - 1]}"
        name = shown(sprint.title, MAX_TITLE)
        lines.append(messages.SUMMARY_SPRINT.format(name=name, due=due, count=count))
    waiting = sum(1 for pull in pulls if _waits_for_review(pull))
    if waiting:
        lines.append(messages.SUMMARY_PRS.format(count=waiting))
    return "\n".join(lines)


def _named(issues: Sequence[Issue]) -> str:
    named = ", ".join(f'#{i.number} "{shown(i.title, MAX_TITLE)}"' for i in issues[:MAX_NEW])
    if len(issues) > MAX_NEW:
        named += messages.SUMMARY_MORE.format(count=len(issues) - MAX_NEW)
    return named


def _open_line(issues: Sequence[Issue], statuses: StatusLabels, capped: str) -> str:
    counts = []
    for status in STATUSES:
        count = sum(1 for i in issues if any(statuses.status_of(n) == status for n in i.labels))
        if count:
            label = shown(shown_status(statuses.label(status)), MAX_TITLE)
            counts.append(f"{label} {count}{capped}")
    unlabelled = sum(1 for i in issues if not any(statuses.is_status(n) for n in i.labels))
    if counts and unlabelled:  # alone, "no status 3" would only repeat the total
        counts.append(f"{messages.SUMMARY_NO_STATUS} {unlabelled}{capped}")
    total = f"{len(issues)}{capped}"
    detail = f" ({', '.join(counts)})" if counts else ""
    return messages.SUMMARY_OPEN.format(count=total, statuses=detail)


def _sprint(milestones: Sequence[Milestone]) -> Milestone | None:
    """The open milestone due soonest, as for "this sprint" (M6.3); only a dated one, since the
    line says when it's due."""
    dated = [m for m in milestones if m.due_on is not None]
    return min(dated, key=lambda m: m.due_on or date.max) if dated else None


def _waits_for_review(pull: PullRequest) -> bool:
    return not pull.draft and pull.approvals == 0 and not pull.changes_requested
