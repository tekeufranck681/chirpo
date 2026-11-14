"""The daily summary (M6.5): what was opened and closed since yesterday, what is open now, the
sprint and the PRs waiting for review, posted once a day at the chat's chosen time."""

from datetime import UTC, date, datetime, time, timedelta

import pytest

from chirpo.core import messages
from chirpo.core.domain import AuditResult, Issue, IssueState, Milestone, RepoRef
from chirpo.core.errors import CodeHostUnavailableError, MessagingError
from chirpo.core.outbound import ZWSP
from chirpo.core.statuses import StatusLabels
from chirpo.core.summary import (
    MAX_TEXT,
    due_day,
    send_due,
    summary_text,
    timezone_named,
    window_start,
)
from chirpo.core.workspaces import SummarySchedule
from tests.fakes import CHAT, FakeCodeHost
from tests.unit.core.test_commands import FRANCK, FRANCK_GITHUB, SANDBOX, World, linked_by_annie

NOW = datetime(2026, 11, 13, 9, 2, tzinfo=UTC)  # a Friday
NINE = SummarySchedule(at=time(9, 0))
FRIDAY = "Fri 13 Nov"


# ---------------------------------------------------------------------------- when


def test_due_at_the_chosen_local_time() -> None:
    assert due_day(NINE, "UTC", None, NOW) == date(2026, 11, 13)


def test_not_due_before_the_time_or_two_hours_after_it() -> None:
    assert due_day(NINE, "UTC", None, NOW.replace(hour=8, minute=59)) is None
    assert due_day(NINE, "UTC", None, NOW.replace(hour=11, minute=0)) is None
    assert due_day(NINE, "UTC", None, NOW.replace(hour=10, minute=59)) == date(2026, 11, 13)


def test_not_due_twice_the_same_day() -> None:
    assert due_day(NINE, "UTC", date(2026, 11, 13), NOW) is None
    assert due_day(NINE, "UTC", date(2026, 11, 12), NOW) == date(2026, 11, 13)


def test_the_time_is_the_teams_own() -> None:
    # 09:00 in Douala (UTC+1) is 08:00 UTC
    assert due_day(NINE, "Africa/Douala", None, NOW.replace(hour=8, minute=1)) is not None
    assert due_day(NINE, "Africa/Douala", None, NOW) is not None  # 10:02 there, still in time
    assert due_day(NINE, "Asia/Tokyo", None, NOW) is None  # 18:02 there


def test_weekdays_only_skips_the_weekend() -> None:
    weekdays = SummarySchedule(at=time(9, 0), weekdays_only=True)
    saturday = NOW + timedelta(days=1)
    assert due_day(weekdays, "UTC", None, saturday) is None
    assert due_day(NINE, "UTC", None, saturday) == date(2026, 11, 14)
    assert due_day(weekdays, "UTC", None, NOW) == date(2026, 11, 13)


def test_a_late_slot_keeps_its_window_after_midnight() -> None:
    late = SummarySchedule(at=time(23, 30))
    just_after = datetime(2026, 11, 14, 0, 15, tzinfo=UTC)
    assert due_day(late, "UTC", None, just_after) == date(2026, 11, 13)
    assert due_day(late, "UTC", date(2026, 11, 13), just_after) is None


def test_summer_time_changes_keep_the_local_time() -> None:
    nine = SummarySchedule(at=time(9, 0))
    # Paris: 09:00 is 08:00 UTC in winter, 07:00 UTC in summer
    assert due_day(nine, "Europe/Paris", None, datetime(2026, 3, 30, 7, 1, tzinfo=UTC))
    assert due_day(nine, "Europe/Paris", None, datetime(2026, 3, 27, 8, 1, tzinfo=UTC))
    assert not due_day(nine, "Europe/Paris", None, datetime(2026, 3, 30, 6, 59, tzinfo=UTC))
    # 02:30 doesn't exist on 29 March 2026 in Paris: it still goes out that morning
    night = SummarySchedule(at=time(2, 30))
    assert due_day(night, "Europe/Paris", None, datetime(2026, 3, 29, 1, 5, tzinfo=UTC))


def test_a_monday_summary_covers_the_weekend_when_weekends_are_skipped() -> None:
    weekdays = SummarySchedule(at=time(9, 0), weekdays_only=True)
    monday = date(2026, 11, 16)
    assert window_start(weekdays, "UTC", monday) == datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
    assert window_start(NINE, "UTC", monday) == datetime(2026, 11, 15, 9, 0, tzinfo=UTC)


def test_timezone_names_are_checked() -> None:
    assert timezone_named("africa/douala") == "Africa/Douala"
    assert timezone_named("UTC") == "UTC"
    assert timezone_named("Mars/Olympus") is None
    assert timezone_named("../etc/passwd") is None


# ---------------------------------------------------------------------------- what


def issue(number: int, title: str, *, hours_ago: float, **fields: object) -> Issue:
    at = NOW - timedelta(hours=hours_ago)
    values: dict[str, object] = {
        "repo": SANDBOX,
        "number": number,
        "title": title,
        "state": IssueState.OPEN,
        "url": f"https://fake.example/{number}",
        "created_at": at,
        "updated_at": at,
        **fields,
    }
    return Issue(**values)  # type: ignore[arg-type]


def github(*issues: Issue) -> FakeCodeHost:
    codehost = FakeCodeHost()
    for found in issues:
        codehost.issues[(found.repo.key, found.number)] = found
    return codehost


async def test_an_active_day() -> None:
    closed = issue(
        3,
        "CSV export",
        hours_ago=72,
        state=IssueState.CLOSED,
        closed_at=NOW - timedelta(hours=2),
        updated_at=NOW - timedelta(hours=2),
    )
    codehost = github(
        issue(1, "Checkout crashes", hours_ago=200, labels=("WIP",)),
        issue(2, "Dark mode", hours_ago=3, labels=("Needs review",), milestone=12),
        closed,
    )
    codehost.milestones[SANDBOX.key] = [
        Milestone(number=12, title="Sprint 12", due_on=date(2026, 11, 20))
    ]
    codehost.add_pull_request(SANDBOX)
    codehost.add_pull_request(SANDBOX, approvals=1)
    codehost.add_pull_request(SANDBOX, draft=True)
    labels = StatusLabels(in_progress="WIP", review="Needs review")
    text = await summary_text([SANDBOX], codehost, labels, NOW, "UTC")
    assert text.splitlines() == [
        messages.SUMMARY_HEADING.format(repo=SANDBOX.full_name, day=FRIDAY),
        messages.SUMMARY_CHANGES.format(when="yesterday", opened=1, closed=1),
        messages.SUMMARY_NEW.format(issues='#2 "Dark mode"'),
        messages.SUMMARY_OPEN.format(count=2, statuses=" (WIP 1, Needs review 1)"),
        messages.SUMMARY_SPRINT.format(name="Sprint 12", due="20 Nov", count=1),
        messages.SUMMARY_PRS.format(count=1),
    ]


async def test_a_quiet_day_still_says_so_with_what_is_open() -> None:
    codehost = github(issue(1, "Checkout crashes", hours_ago=200))
    text = await summary_text([SANDBOX], codehost, StatusLabels(), NOW, "UTC")
    assert text.splitlines() == [
        messages.SUMMARY_HEADING.format(repo=SANDBOX.full_name, day=FRIDAY),
        messages.SUMMARY_QUIET.format(when="yesterday"),
        messages.SUMMARY_OPEN.format(count=1, statuses=""),
    ]


async def test_many_new_issues_are_counted_not_all_listed() -> None:
    codehost = github(*(issue(n, f"Issue {n}", hours_ago=1) for n in range(1, 9)))
    text = await summary_text([SANDBOX], codehost, StatusLabels(), NOW, "UTC")
    new = text.splitlines()[2]
    assert new.count("#") == 5 and new.endswith(messages.SUMMARY_MORE.format(count=3))


async def test_titles_cannot_mention_anyone() -> None:
    codehost = github(issue(1, "Ping @annie_n and tap /unlink", hours_ago=1))
    text = await summary_text([SANDBOX], codehost, StatusLabels(), NOW, "UTC")
    assert "@annie_n" not in text and f"@{ZWSP}annie_n" in text and f"/{ZWSP}unlink" in text


async def test_each_repo_has_its_own_part_and_trouble_is_said() -> None:
    other = RepoRef.parse("github", "tekeufranck681/chirpo")
    codehost = github(issue(1, "Checkout crashes", hours_ago=1))
    real = codehost.list_issues

    async def flaky(repo: RepoRef, query: object) -> list[Issue]:
        if repo.key == other.key:
            raise CodeHostUnavailableError("down")
        return await real(repo, query)  # type: ignore[arg-type]

    codehost.list_issues = flaky  # type: ignore[method-assign]
    text = await summary_text([SANDBOX, other], codehost, StatusLabels(), NOW, "UTC")
    parts = text.split("\n\n")
    heading = messages.SUMMARY_HEADING.format(repo=SANDBOX.full_name, day=FRIDAY)
    assert parts[0].startswith(heading)
    assert parts[1] == messages.SUMMARY_UNAVAILABLE.format(repo=other.full_name)


async def test_a_monday_says_since_friday() -> None:
    monday = datetime(2026, 11, 16, 9, 1, tzinfo=UTC)
    friday = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
    codehost = github(issue(1, "Weekend crash", hours_ago=-50))  # Saturday morning
    text = await summary_text([SANDBOX], codehost, StatusLabels(), monday, "UTC", since=friday)
    assert text.splitlines()[1] == messages.SUMMARY_CHANGES.format(
        when="Friday", opened=1, closed=0
    )


async def test_issues_without_a_status_are_counted_next_to_the_others() -> None:
    codehost = github(
        issue(6, "Login button broken", hours_ago=100, labels=("status: in progress",)),
        issue(7, "Checkout crashes", hours_ago=100, labels=("bug",)),
        issue(8, "Checkout page broken", hours_ago=100),
    )
    text = await summary_text([SANDBOX], codehost, StatusLabels(), NOW, "UTC")
    assert messages.SUMMARY_OPEN.format(count=3, statuses=" (in progress 1, no status 2)") in text


async def test_counts_from_a_capped_list_say_so() -> None:
    many = [issue(n, f"Old {n}", hours_ago=500, labels=("status: todo",)) for n in range(1, 101)]
    text = await summary_text([SANDBOX], github(*many), StatusLabels(), NOW, "UTC")
    assert messages.SUMMARY_OPEN.format(count="100+", statuses=" (todo 100+)") in text


async def test_a_summary_never_exceeds_one_message() -> None:
    repos = [RepoRef.parse("github", f"acme/repo-{n}") for n in range(10)]
    codehost = FakeCodeHost()
    for repo in repos:
        for n in range(1, 8):
            found = issue(n, "x" * 300, hours_ago=1, labels=("y" * 50,))
            codehost.issues[(repo.key, n)] = found.model_copy(update={"repo": repo})
    text = await summary_text(repos, codehost, StatusLabels(todo="y" * 50), NOW, "UTC")
    assert len(text) == MAX_TEXT and text.endswith("…")


async def test_the_day_is_the_teams_local_day() -> None:
    late = NOW.replace(hour=23, minute=30)  # already Saturday in Tokyo
    text = await summary_text([SANDBOX], github(), StatusLabels(), late, "Asia/Tokyo")
    assert "Sat 14 Nov" in text.splitlines()[0]


# ---------------------------------------------------------------------------- sending


async def scheduled(world: World, schedule: SummarySchedule = NINE) -> None:
    await linked_by_annie(world)
    await world.workspaces.set_summary_schedule(CHAT, schedule)
    world.messaging.sent.clear()


async def test_a_due_summary_is_posted_once() -> None:
    world = World()
    await scheduled(world)
    messengers = {CHAT.platform: world.messaging}
    assert await send_due(world.services, messengers, NOW) == 1
    [(chat, text, _)] = world.messaging.sent
    assert chat == CHAT and text.startswith("Daily summary for")
    assert await send_due(world.services, messengers, NOW + timedelta(minutes=5)) == 0
    assert len(world.messaging.sent) == 1


async def test_nothing_is_posted_before_the_time() -> None:
    world = World()
    await scheduled(world, SummarySchedule(at=time(17, 30)))
    assert await send_due(world.services, {CHAT.platform: world.messaging}, NOW) == 0
    assert world.messaging.sent == []


async def test_a_chat_the_bot_was_removed_from_is_not_tried_every_minute() -> None:
    world = World()
    await scheduled(world)
    world.messaging.unreachable_chats.add(CHAT.chat_id)
    messengers = {CHAT.platform: world.messaging}
    assert await send_due(world.services, messengers, NOW) == 0
    world.messaging.unreachable_chats.clear()
    assert await send_due(world.services, messengers, NOW + timedelta(minutes=1)) == 0
    entry = world.audit.entries[-1]
    assert (entry.action, entry.result, entry.error) == (
        "post_summary",
        AuditResult.FAILED,
        "chat_unreachable",
    )


async def test_a_failure_that_may_pass_is_tried_again_the_next_minute() -> None:
    world = World()
    await scheduled(world)
    messengers = {CHAT.platform: world.messaging}
    real = world.messaging.send_message
    calls = 0

    async def flaky(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MessagingError("timeout")
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    world.messaging.send_message = flaky  # type: ignore[method-assign]
    assert await send_due(world.services, messengers, NOW) == 0
    assert await send_due(world.services, messengers, NOW + timedelta(minutes=1)) == 1
    assert [e.result for e in world.audit.entries] == [AuditResult.FAILED, AuditResult.EXECUTED]


async def test_each_post_is_audited_without_a_person() -> None:
    world = World()
    await scheduled(world)
    await send_due(world.services, {CHAT.platform: world.messaging}, NOW)
    [entry] = world.audit.entries
    assert (entry.action, entry.result, entry.actor_member_id) == (
        "post_summary",
        AuditResult.EXECUTED,
        None,
    )
    assert entry.payload == {"day": "2026-11-13", "repos": [SANDBOX.full_name]}


async def test_a_chat_on_a_platform_that_is_not_running_is_skipped() -> None:
    world = World()
    await scheduled(world)
    assert await send_due(world.services, {}, NOW) == 0


# ---------------------------------------------------------------------------- settings


@pytest.fixture
def world() -> World:
    return World()


async def test_an_admin_turns_the_summary_on_and_off(world: World) -> None:
    await linked_by_annie(world)
    reply = await world.say("/settings summary 9:00 weekdays")
    assert reply == messages.SUMMARY_SET.format(
        at="09:00", days=messages.SUMMARY_WEEKDAYS, timezone="UTC"
    )
    assert await world.workspaces.summary_schedule(CHAT) == SummarySchedule(
        at=time(9, 0), weekdays_only=True
    )
    assert world.audit.entries[0].payload == {"summary": "09:00 weekdays", "was": None}
    shown = messages.SUMMARY_AT.format(at="09:00", days=messages.SUMMARY_WEEKDAYS)
    assert f"Daily summary: {shown}" in await world.say("/settings")
    assert await world.say("/settings summary off") == messages.SUMMARY_OFF
    assert await world.workspaces.summary_schedule(CHAT) is None


@pytest.mark.parametrize("said", ["25:00", "9", "nine", "09:00 sometimes", ""])
async def test_a_bad_time_shows_the_usage(world: World, said: str) -> None:
    await linked_by_annie(world)
    assert await world.say(f"/settings summary {said}") == messages.SUMMARY_USAGE
    assert await world.workspaces.summary_schedule(CHAT) is None


async def test_an_admin_sets_the_timezone(world: World) -> None:
    await linked_by_annie(world)
    reply = await world.say("/settings timezone africa/douala")
    assert reply == messages.TIMEZONE_SET.format(timezone="Africa/Douala")
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None and workspace.timezone == "Africa/Douala"
    assert world.audit.entries[0].payload == {"timezone": "Africa/Douala", "was": "UTC"}
    assert "Timezone: Africa/Douala" in await world.say("/settings")


async def test_an_unknown_timezone_is_refused(world: World) -> None:
    await linked_by_annie(world)
    assert await world.say("/settings timezone Mars/Olympus") == messages.TIMEZONE_UNKNOWN
    assert world.audited() == []


async def test_members_cannot_schedule_the_summary(world: World) -> None:
    await linked_by_annie(world)
    await world.verified(FRANCK, FRANCK_GITHUB)
    too_low = messages.ROLE_TOO_LOW.format(role="admin")
    assert await world.say("/settings summary 09:00", FRANCK) == too_low
    assert await world.say("/settings timezone Europe/Paris", FRANCK) == too_low
    assert await world.workspaces.summary_schedule(CHAT) is None


async def test_the_summary_is_off_by_default(world: World) -> None:
    await linked_by_annie(world)
    assert f"Daily summary: {messages.SUMMARY_IS_OFF}" in await world.say("/settings")
