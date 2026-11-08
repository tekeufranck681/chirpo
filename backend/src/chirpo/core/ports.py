"""Protocols implemented outside the core: messaging, code host, LLM, repositories."""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from chirpo.core.domain import (
    ChatRef,
    InboundEvent,
    Issue,
    IssuePatch,
    IssueQuery,
    LabelSpec,
    MergeMethod,
    MergeResult,
    NewIssue,
    PullRequest,
    PullRequestQuery,
    RepoPermission,
    RepoRef,
    SentMessage,
)


class MessagingAdapter(Protocol):
    platform: str
    # Names the stream event ids come from (platform + bot account), e.g. "telegram:7000000001".
    event_source: str

    def parse_update(self, raw: Mapping[str, Any]) -> InboundEvent | None:
        """Normalise a platform payload. None means "not something chirpo handles"."""
        ...

    async def send_message(
        self, chat: ChatRef, text: str, *, reply_to: str | None = None
    ) -> SentMessage: ...

    async def edit_message(self, message: SentMessage, text: str) -> None: ...

    async def aclose(self) -> None: ...


class ProcessedEvents(Protocol):
    async def claim(self, source: str, external_id: str) -> bool:
        """Record an event as processed. False if it already was (a duplicate delivery)."""
        ...


class PollCursors(Protocol):
    async def get(self, source: str, scope: str) -> str | None: ...

    async def set(self, source: str, scope: str, cursor: str) -> None: ...


class CodeHostAdapter(Protocol):
    """GitHub today; GitLab and Linear later. Raises chirpo.core.errors.CodeHost* errors.

    - Issue methods never return pull requests (GitHub's issues API mixes them in: filter them out);
      `get_issue` on a PR number raises CodeHostNotFoundError.
    - Usernames and repo names are case-insensitive (`same_login`, `RepoRef.key`).
    - `permission_of` maps the host's own access levels onto RepoPermission.
    - `merge_pull_request` raises CodeHostConflictError when the PR can't be merged.
    """

    provider: str

    async def create_issue(self, repo: RepoRef, issue: NewIssue) -> Issue: ...

    async def get_issue(self, repo: RepoRef, number: int) -> Issue: ...

    async def update_issue(self, repo: RepoRef, number: int, patch: IssuePatch) -> Issue: ...

    async def list_issues(self, repo: RepoRef, query: IssueQuery) -> list[Issue]: ...

    async def add_comment(self, repo: RepoRef, number: int, body: str) -> str:
        """Returns the comment's URL."""
        ...

    async def ensure_labels(self, repo: RepoRef, labels: Sequence[LabelSpec]) -> None:
        """Create the labels that don't exist yet; existing ones are left untouched."""
        ...

    async def user_exists(self, username: str) -> bool: ...

    async def permission_of(self, repo: RepoRef, username: str) -> RepoPermission: ...

    async def list_pull_requests(
        self, repo: RepoRef, query: PullRequestQuery
    ) -> list[PullRequest]: ...

    async def get_pull_request(self, repo: RepoRef, number: int) -> PullRequest: ...

    async def merge_pull_request(
        self, repo: RepoRef, number: int, method: MergeMethod
    ) -> MergeResult: ...

    async def aclose(self) -> None: ...
