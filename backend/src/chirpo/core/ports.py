"""Protocols implemented outside the core: messaging, code host, LLM, repositories."""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from uuid import UUID

from chirpo.core.domain import (
    ChatRef,
    ChatUser,
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
from chirpo.core.permissions import Role
from chirpo.core.workspaces import AuditEntry, CodeHostAccount, LinkedRepo, Member, Workspace


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
    - Assignees the host can't assign (e.g. no access to the repo) may be dropped silently: the
      returned issue lists who was really assigned, so compare before telling the chat.
    - A repo the app can't reach (not installed, access removed) raises CodeHostForbiddenError.
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

    async def user_exists(self, repo: RepoRef, username: str) -> bool:
        """Whether the account exists. `repo` says which installation to ask through."""
        ...

    async def permission_of(self, repo: RepoRef, username: str) -> RepoPermission: ...

    async def current_login(self, repo: RepoRef, user_id: str) -> str | None:
        """The account's login today (logins can be renamed and then taken by someone else).
        None if the account no longer exists. `repo` says which installation to ask through."""
        ...

    async def list_pull_requests(
        self, repo: RepoRef, query: PullRequestQuery
    ) -> list[PullRequest]: ...

    async def get_pull_request(self, repo: RepoRef, number: int) -> PullRequest: ...

    async def merge_pull_request(
        self, repo: RepoRef, number: int, method: MergeMethod
    ) -> MergeResult: ...

    async def aclose(self) -> None: ...


class Workspaces(Protocol):
    """A chat belongs to at most one workspace, which has linked repos (one is the default)."""

    async def for_chat(self, chat: ChatRef) -> Workspace | None: ...

    async def get_or_create_for_chat(self, chat: ChatRef, *, name: str) -> Workspace: ...

    async def repos(self, workspace_id: UUID) -> list[LinkedRepo]:
        """The default repo first, then the others in the order they were linked."""
        ...

    async def link_repo(
        self, workspace_id: UUID, repo: RepoRef, *, installation_id: int | None = None
    ) -> LinkedRepo:
        """The first linked repo becomes the default. Linking again returns the existing link."""
        ...

    async def unlink_repo(self, workspace_id: UUID, repo: RepoRef) -> bool:
        """False if it wasn't linked. Unlinking the default promotes the oldest remaining repo."""
        ...


class Members(Protocol):
    async def for_chat_user(self, workspace_id: UUID, user: ChatUser) -> Member:
        """The member behind this chat account, created as a plain member on first contact."""
        ...

    async def all(self, workspace_id: UUID) -> list[Member]: ...

    async def set_role(self, workspace_id: UUID, member_id: UUID, role: Role) -> Member | None: ...

    async def link_codehost(
        self, workspace_id: UUID, member_id: UUID, account: CodeHostAccount
    ) -> Member | None:
        """Attach a verified account, replacing the member's previous one. If another member had
        this account, it moves to this member (e.g. the person changed chat account)."""
        ...


class AuditLog(Protocol):
    async def record(self, entry: AuditEntry) -> None: ...
