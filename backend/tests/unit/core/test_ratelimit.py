from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from chirpo.core.errors import CountersUnavailableError
from chirpo.core.ratelimit import HOUR, Limits, LimitVerdict, RateLimiter
from tests.fakes import FakeCounters

TEAM, FRANCK, ANNIE = uuid4(), uuid4(), uuid4()
NOON = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOON

    def __call__(self) -> datetime:
        return self.now


def limiter(clock: Clock | None = None, **limits: int) -> tuple[RateLimiter, FakeCounters]:
    counters = FakeCounters()
    values = {
        "llm_per_member_hour": 3,
        "llm_per_workspace_hour": 5,
        "llm_tokens_per_day": 0,
        "github_writes_per_member_hour": 2,
    } | limits
    return RateLimiter(counters, Limits(**values), clock or Clock()), counters


async def test_requests_pass_until_the_limit() -> None:
    rates, _ = limiter()
    for _ in range(3):
        assert (await rates.check_llm(TEAM, FRANCK)).allowed


async def test_the_first_request_over_the_limit_is_told_and_the_rest_are_silent() -> None:
    rates, _ = limiter()
    for _ in range(3):
        await rates.check_llm(TEAM, FRANCK)
    told = await rates.check_llm(TEAM, FRANCK)
    assert (told.allowed, told.notify, told.scope) == (False, True, "member")
    assert told.retry_after == HOUR  # noon sharp: a full hour to the next window
    quiet = await rates.check_llm(TEAM, FRANCK)
    assert (quiet.allowed, quiet.notify) == (False, False)


async def test_the_limit_resets_with_the_next_window() -> None:
    clock = Clock()
    rates, _ = limiter(clock)
    for _ in range(4):
        await rates.check_llm(TEAM, FRANCK)
    clock.now = NOON + timedelta(hours=1)
    assert (await rates.check_llm(TEAM, FRANCK)).allowed


async def test_retry_after_is_the_time_left_in_the_window() -> None:
    clock = Clock()
    clock.now = NOON + timedelta(minutes=47, seconds=30)
    rates, _ = limiter(clock, llm_per_member_hour=1)
    await rates.check_llm(TEAM, FRANCK)
    assert (await rates.check_llm(TEAM, FRANCK)).retry_after == 12 * 60 + 30


async def test_people_are_limited_separately_and_the_team_as_a_whole() -> None:
    rates, _ = limiter(llm_per_member_hour=3, llm_per_workspace_hour=4)
    for _ in range(3):
        assert (await rates.check_llm(TEAM, FRANCK)).allowed
    assert (await rates.check_llm(TEAM, ANNIE)).allowed  # her own allowance: the team's 4th
    team = await rates.check_llm(TEAM, ANNIE)
    assert (team.allowed, team.scope) == (False, "workspace")
    other_team = await rates.check_llm(uuid4(), uuid4())
    assert other_team.allowed


async def test_a_limited_person_does_not_use_up_the_teams_allowance() -> None:
    rates, counters = limiter(llm_per_member_hour=1, llm_per_workspace_hour=10)
    for _ in range(6):
        await rates.check_llm(TEAM, FRANCK)
    assert (await rates.check_llm(TEAM, ANNIE)).allowed
    assert sum(v for k, v in counters.values.items() if ":llm_workspace:" in k) == 2


async def test_zero_turns_a_limit_off() -> None:
    rates, counters = limiter(llm_per_member_hour=0, llm_per_workspace_hour=0)
    for _ in range(50):
        assert (await rates.check_llm(TEAM, FRANCK)).allowed
    assert counters.values == {}


async def test_the_daily_token_budget() -> None:
    rates, _ = limiter(llm_tokens_per_day=10_000)
    assert (await rates.check_llm(TEAM, FRANCK)).allowed
    await rates.add_tokens(TEAM, 6_000)
    assert (await rates.check_llm(TEAM, FRANCK)).allowed
    await rates.add_tokens(TEAM, 4_000)
    over = await rates.check_llm(TEAM, ANNIE)
    assert (over.allowed, over.scope, over.notify) == (False, "budget", True)
    assert over.retry_after == 12 * HOUR  # to midnight UTC


async def test_budget_refusals_are_told_once_and_use_no_hourly_allowance() -> None:
    rates, counters = limiter(llm_tokens_per_day=100)
    await rates.add_tokens(TEAM, 100)
    first = await rates.check_llm(TEAM, FRANCK)
    again = await rates.check_llm(TEAM, ANNIE)
    assert (first.notify, again.notify) == (True, False)
    assert not any(":llm_member:" in key or ":llm_workspace:" in key for key in counters.values)


async def test_no_budget_means_tokens_are_not_counted() -> None:
    rates, counters = limiter(llm_tokens_per_day=0)
    await rates.add_tokens(TEAM, 5_000)
    assert counters.values == {}


async def test_github_writes_are_counted_per_person() -> None:
    rates, _ = limiter(github_writes_per_member_hour=2)
    assert (await rates.check_write(FRANCK)).allowed
    assert (await rates.check_write(FRANCK)).allowed
    blocked = await rates.check_write(FRANCK)
    assert (blocked.allowed, blocked.notify, blocked.scope) == (False, True, "write")
    assert (await rates.check_write(ANNIE)).allowed


async def test_requests_and_writes_have_their_own_counters() -> None:
    rates, _ = limiter(llm_per_member_hour=1, github_writes_per_member_hour=1)
    await rates.check_llm(TEAM, FRANCK)
    assert (await rates.check_write(FRANCK)).allowed


async def test_a_broken_counter_store_lets_everything_through() -> None:
    rates, counters = limiter()
    counters.fail = CountersUnavailableError("redis down")
    assert (await rates.check_llm(TEAM, FRANCK)).allowed
    assert (await rates.check_write(FRANCK)).allowed
    await rates.add_tokens(TEAM, 100)  # no error either


def test_verdict_allowed_has_nothing_to_say() -> None:
    assert LimitVerdict.ok() == LimitVerdict(allowed=True)


@pytest.mark.parametrize("bad", [-1])
def test_limits_cannot_be_negative(bad: int) -> None:
    with pytest.raises(ValueError):
        Limits(
            llm_per_member_hour=bad,
            llm_per_workspace_hour=0,
            llm_tokens_per_day=0,
            github_writes_per_member_hour=0,
        )
