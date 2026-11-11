"""Protocols implemented outside the core: messaging, code host, LLM, repositories."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from chirpo.core.domain import (
    Attachment,
    Button,
    ChatRef,
    ChatUser,
    InboundCallback,
    InboundEvent,
    InboundMessage,
    Issue,
    IssuePatch,
    IssueQuery,
    LabelSpec,
    MergeMethod,
    MergeResult,
    NewIssue,
    PullRequest,
    PullRequestQuery,
    RecentMessage,
    RepoPermission,
    RepoRef,
    SentMessage,
)
from chirpo.core.llm import Interpretation, InterpretRequest
from chirpo.core.pending import PendingAction, PendingStatus, Step
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
        self,
        chat: ChatRef,
        text: str,
        *,
        reply_to: str | None = None,
        buttons: Sequence[Button] = (),
    ) -> SentMessage: ...

    async def edit_message(
        self, message: SentMessage, text: str, *, buttons: Sequence[Button] = ()
    ) -> None:
        """Replace the text. The buttons are replaced too: none given means they are removed."""
        ...

    async def answer_callback(self, callback: InboundCallback, text: str | None = None) -> None:
        """Acknowledge a button press, with a short note only the person who pressed sees.
        Best effort: the platform drops old presses."""
        ...

    async def show_typing(self, chat: ChatRef) -> None:
        """Show that the bot is working on an answer (a few seconds; best effort)."""
        ...

    async def download(self, attachment: Attachment, *, max_bytes: int) -> bytes:
        """The file's bytes. Raises `AttachmentTooLargeError` when a size the platform reported is
        over `max_bytes` (before downloading), or when the bytes received are (after), and
        `AttachmentUnavailableError` when the platform no longer has the file."""
        ...

    def private_chat(self, user: ChatUser) -> ChatRef:
        """The one-to-one chat between the bot and this user."""
        ...

    def private_chat_link(self) -> str:
        """A link that opens a private chat with the bot (it may not message people first)."""
        ...

    def bot_mention(self) -> str:
        """How people address the bot in a group, e.g. "@chirpo_dev_bot"."""
        ...

    async def is_chat_admin(self, chat: ChatRef, user: ChatUser) -> bool:
        """Whether the user runs this group (its creator or an administrator)."""
        ...

    async def aclose(self) -> None: ...


class ProcessedEvents(Protocol):
    async def claim(self, source: str, external_id: str) -> bool:
        """Record an event as processed. False if it already was (a duplicate delivery)."""
        ...

    async def release(self, source: str, external_id: str) -> None:
        """Forget a claim, so a retry of an event cut off mid-way is handled again."""
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

    async def list_labels(self, repo: RepoRef) -> list[str]:
        """The repo's label names, spelled as on the code host."""
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
        self,
        workspace_id: UUID,
        repo: RepoRef,
        *,
        installation_id: int | None = None,
        first_admin: UUID | None = None,
    ) -> LinkedRepo | None:
        """The first linked repo becomes the default. Linking again returns the existing link.

        With `first_admin`, the link is made only if the workspace has no repo yet, and that member
        becomes admin in the same step; None means another repo got there first.
        """
        ...

    async def unlink_repo(self, workspace_id: UUID, repo: RepoRef) -> bool:
        """False if it wasn't linked. Unlinking the default promotes the oldest remaining repo."""
        ...

    async def set_keeps_context(self, workspace_id: UUID, *, keep: bool) -> None: ...


class Members(Protocol):
    async def for_chat_user(self, workspace_id: UUID, user: ChatUser) -> Member:
        """The member behind this chat account, created as a plain member on first contact."""
        ...

    async def all(self, workspace_id: UUID) -> list[Member]: ...

    async def set_role(self, workspace_id: UUID, member_id: UUID, role: Role) -> Member | None:
        """None if no such member. Raises LastAdminError rather than leave no admin."""
        ...

    async def link_codehost(
        self, workspace_id: UUID, member_id: UUID, account: CodeHostAccount
    ) -> Member | None:
        """Attach a verified account, replacing the member's previous one. If another member had
        this account, it moves to this member (e.g. the person changed chat account)."""
        ...


class AuditLog(Protocol):
    async def record(self, entry: AuditEntry) -> None: ...


class LLMProvider(Protocol):
    model: str

    async def interpret(self, request: InterpretRequest) -> Interpretation:
        """Proposals for the message, validated against the action models. Raises `LLMError`s."""
        ...


class PendingActions(Protocol):
    """Proposals waiting for confirmation. Every call is scoped to a workspace (D8)."""

    async def create(
        self,
        *,
        workspace_id: UUID,
        requested_by: UUID,
        chat: ChatRef,
        steps: Sequence[Step],
        expires_at: datetime,
    ) -> PendingAction: ...

    async def attach_message(self, workspace_id: UUID, pending_id: UUID, message_id: str) -> None:
        """Records the card, so a press can be checked against the message it came from."""
        ...

    async def get(self, workspace_id: UUID, pending_id: UUID) -> PendingAction | None: ...

    async def find_by_card(
        self, workspace_id: UUID, chat: ChatRef, message_id: str
    ) -> PendingAction | None:
        """The proposal whose card is this message, for replies to a card."""
        ...

    async def expire_due(self, now: datetime) -> list[PendingAction]:
        """Marks waiting proposals past their time expired, once each, and returns them."""
        ...

    async def fail_stuck(self, before: datetime) -> list[PendingAction]:
        """Marks runs that were confirmed but never finished, untouched since `before`, failed
        (the worker died mid-run), and returns them."""
        ...

    async def purge(self, before: datetime) -> int:
        """Deletes finished proposals untouched since `before`; returns how many."""
        ...

    async def advance(
        self,
        workspace_id: UUID,
        pending_id: UUID,
        *,
        expect: PendingStatus,
        to: PendingStatus,
        now: datetime,
        by: UUID | None = None,
        result: dict[str, Any] | None = None,
    ) -> PendingAction | None:
        """Moves a proposal from `expect` to `to`, atomically: None when it isn't in `expect`
        any more (someone else got there first), or, leaving `proposed` for anything but `expired`,
        when it has expired. Only one of two simultaneous presses wins."""
        ...


class Counters(Protocol):
    """Counters that expire by themselves, for rate limits. Raises `CountersUnavailableError`."""

    async def add(self, key: str, amount: int, ttl: int) -> int:
        """Adds to the counter, which disappears `ttl` seconds later. Returns the new total."""
        ...

    async def get(self, key: str) -> int: ...


class MessageBuffer(Protocol):
    """Recent chat messages kept for context, encrypted, for a limited time (D46)."""

    async def remember(self, workspace_id: UUID, message: InboundMessage) -> None:
        """Keep the message; a second delivery of the same message is ignored."""
        ...

    async def recent(
        self, workspace_id: UUID, chat: ChatRef, *, limit: int = 20
    ) -> list[RecentMessage]:
        """The chat's last `limit` messages still within the retention time, oldest first."""
        ...

    async def prune(self) -> int:
        """Delete messages older than the retention time; returns how many."""
        ...

    async def find(
        self, workspace_id: UUID, chat: ChatRef, message_id: str
    ) -> RecentMessage | None:
        """One kept message, e.g. the one a request replies to; None if unknown or expired."""
        ...

    async def forget(self, workspace_id: UUID) -> int:
        """Delete everything kept for the workspace; returns how many."""
        ...
