"""Limits on how much one person or team can ask of the model and of GitHub (D49).

They are a safety net against loops, floods and a shared quota being used up by one person, not a
cap on normal use: only requests addressed to the bot count, and a refused request costs nothing.
Windows are fixed (a counter per clock hour or UTC day), which makes each check one atomic
increment. If the counter store is down, everything is let through: a broken counter must not stop
the bot.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from chirpo.core.errors import CountersUnavailableError
from chirpo.core.ports import Counters

HOUR = 3600
DAY = 24 * HOUR

log = logging.getLogger(__name__)
Scope = Literal["member", "workspace", "budget", "write"]


@dataclass(frozen=True, slots=True)
class Limits:
    """0 turns a limit off."""

    llm_per_member_hour: int
    llm_per_workspace_hour: int
    llm_tokens_per_day: int
    github_writes_per_member_hour: int

    def __post_init__(self) -> None:
        if (
            min(
                self.llm_per_member_hour,
                self.llm_per_workspace_hour,
                self.llm_tokens_per_day,
                self.github_writes_per_member_hour,
            )
            < 0
        ):
            raise ValueError("limits can't be negative")


@dataclass(frozen=True, slots=True)
class LimitVerdict:
    allowed: bool
    scope: Scope | None = None
    retry_after: int = 0  # seconds until the window ends
    notify: bool = False  # only the first refusal in a window is answered

    @classmethod
    def ok(cls) -> "LimitVerdict":
        return cls(allowed=True)


class RateLimiter:
    def __init__(
        self,
        counters: Counters,
        limits: Limits,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._counters = counters
        self.limits = limits
        self._now = now

    async def check_llm(self, workspace_id: UUID, member_id: UUID) -> LimitVerdict:
        """Counts a request to the model for the person and the team; checks the token budget."""
        try:
            if self.limits.llm_tokens_per_day:  # first: a refused request uses no hourly allowance
                used = await self._counters.get(self._key("tokens", workspace_id, DAY))
                if used >= self.limits.llm_tokens_per_day:
                    told = await self._counters.add(
                        self._key("budget_told", workspace_id, DAY), 1, DAY
                    )
                    return LimitVerdict(False, "budget", self._left(DAY), notify=told == 1)
            verdict = await self._hit(
                "llm_member", member_id, self.limits.llm_per_member_hour, HOUR, "member"
            )
            if verdict.allowed:  # a limited person doesn't use up the team's allowance
                verdict = await self._hit(
                    "llm_workspace",
                    workspace_id,
                    self.limits.llm_per_workspace_hour,
                    HOUR,
                    "workspace",
                )
            return verdict
        except CountersUnavailableError:
            log.warning("rate_limit_store_unavailable")
            return LimitVerdict.ok()

    async def check_write(self, member_id: UUID) -> LimitVerdict:
        """Counts a write to GitHub for the person."""
        try:
            return await self._hit(
                "github_write", member_id, self.limits.github_writes_per_member_hour, HOUR, "write"
            )
        except CountersUnavailableError:
            log.warning("rate_limit_store_unavailable")
            return LimitVerdict.ok()

    async def add_tokens(self, workspace_id: UUID, tokens: int) -> None:
        if not self.limits.llm_tokens_per_day or tokens <= 0:
            return
        try:
            await self._counters.add(self._key("tokens", workspace_id, DAY), tokens, DAY)
        except CountersUnavailableError:
            log.warning("rate_limit_store_unavailable")

    async def _hit(
        self, kind: str, who: UUID, limit: int, window: int, scope: Scope
    ) -> LimitVerdict:
        if not limit:
            return LimitVerdict.ok()
        count = await self._counters.add(self._key(kind, who, window), 1, window)
        if count <= limit:
            return LimitVerdict.ok()
        return LimitVerdict(False, scope, self._left(window), notify=count == limit + 1)

    def _key(self, kind: str, who: UUID, window: int) -> str:
        return f"chirpo:rl:{kind}:{who}:{int(self._now().timestamp()) // window}"

    def _left(self, window: int) -> int:
        return window - int(self._now().timestamp()) % window
