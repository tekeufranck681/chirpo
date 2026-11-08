"""Domain models (pure Pydantic, no I/O): InboundMessage, Workspace, Member, Issue, ...

Models arrive with the features that need them.
"""

import re
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class IdentityKind(StrEnum):
    """Which kind of account an identity is: a chat account or a code host account."""

    CHAT = "chat"
    CODEHOST = "codehost"


class AuditResult(StrEnum):
    """Outcome of an action, as recorded in the audit log."""

    EXECUTED = "executed"
    FAILED = "failed"
    DENIED = "denied"
    REJECTED = "rejected"


class ChatRef(BaseModel):
    """Where a message lives. `thread_id` is a topic inside a group (Telegram forum topics)."""

    model_config = ConfigDict(frozen=True)

    platform: str
    chat_id: str
    thread_id: str | None = None


class ChatUser(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: str
    user_id: str
    display_name: str
    username: str | None = None
    is_bot: bool = False
    # Posted "as the chat" (e.g. anonymous group admin): no individual identity, so no privileges.
    is_anonymous: bool = False


class InboundMessage(BaseModel):
    """A chat message, normalised by a messaging adapter. `event_id` is unique per platform.

    `text` has mentions of the bot removed (then `mentions_bot` is True); a media caption counts as
    text, and media without a caption has empty text.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str
    chat: ChatRef
    sender: ChatUser
    message_id: str
    text: str
    sent_at: AwareDatetime
    mentions_bot: bool = False
    replies_to_bot: bool = False
    is_private_chat: bool = False


class InboundCallback(BaseModel):
    """A button press on one of the bot's messages."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    chat: ChatRef
    sender: ChatUser
    message_id: str
    data: str


InboundEvent = InboundMessage | InboundCallback


class SentMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    chat: ChatRef
    message_id: str


# ------------------------------------------------------------ code host (GitHub, later GitLab)

# Owners: letters, digits, hyphens; Enterprise Managed Users also use underscores.
REPO_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
REPO_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")


class RepoRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def key(self) -> str:
        """Case-insensitive identity: GitHub treats Foo/Bar and foo/bar as the same repo."""
        return f"{self.provider}:{self.full_name.lower()}"

    @classmethod
    def parse(cls, provider: str, text: str) -> "RepoRef":
        """Parse "owner/repo" as typed by a user. Raises ValueError if it isn't one."""
        owner, slash, name = text.strip().partition("/")
        if not (slash and REPO_OWNER.fullmatch(owner) and REPO_NAME.fullmatch(name)):
            raise ValueError(f"expected owner/repo, got {text.strip()!r}")
        return cls(provider=provider, owner=owner, name=name)


def same_login(a: str, b: str) -> bool:
    """Code host usernames are case-insensitive."""
    return a.casefold() == b.casefold()


def _non_empty_names(values: tuple[str, ...] | None) -> tuple[str, ...] | None:
    if values is None:
        return None
    cleaned = tuple(value.strip() for value in values)
    if any(not value for value in cleaned):
        raise ValueError("names must not be empty")
    return cleaned


class IssueState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class CloseReason(StrEnum):
    COMPLETED = "completed"
    NOT_PLANNED = "not_planned"
    DUPLICATE = "duplicate"


class Issue(BaseModel):
    model_config = ConfigDict(frozen=True)

    repo: RepoRef
    number: int
    title: str
    body: str = ""
    state: IssueState
    close_reason: CloseReason | None = None
    assignees: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    url: str
    author: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class NewIssue(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=256)
    body: str = ""
    assignees: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()

    _names = field_validator("assignees", "labels")(_non_empty_names)


class IssuePatch(BaseModel):
    """Fields to change; None means "leave as is"."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=256)
    body: str | None = None
    state: IssueState | None = None
    close_reason: CloseReason | None = None
    # Replace the whole list (GitHub semantics), not add to it.
    assignees: tuple[str, ...] | None = None
    labels: tuple[str, ...] | None = None

    _names = field_validator("assignees", "labels")(_non_empty_names)

    @property
    def is_empty(self) -> bool:
        return not self.model_dump(exclude_none=True)


class IssueQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: IssueState | Literal["all"] = IssueState.OPEN
    assignee: str | None = None
    labels: tuple[str, ...] = ()
    limit: int = Field(default=30, ge=1, le=100)


class LabelSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=50)
    color: str = Field(pattern=r"^[0-9a-fA-F]{6}$")
    description: str = ""


class RepoPermission(StrEnum):
    """A person's access to a repo, ranked from none to admin (GitHub's levels)."""

    NONE = "none"
    READ = "read"
    TRIAGE = "triage"
    WRITE = "write"
    MAINTAIN = "maintain"
    ADMIN = "admin"

    def at_least(self, other: "RepoPermission") -> bool:
        return _PERMISSION_RANK[self] >= _PERMISSION_RANK[other]


_PERMISSION_RANK = {permission: rank for rank, permission in enumerate(RepoPermission)}


class PullRequestState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class CheckStatus(StrEnum):
    PASSING = "passing"
    FAILING = "failing"
    PENDING = "pending"
    NONE = "none"


class PullRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    repo: RepoRef
    number: int
    title: str
    state: PullRequestState
    author: str | None = None
    head_branch: str
    base_branch: str
    url: str
    draft: bool = False
    mergeable: bool | None = None  # None: the code host hasn't computed it yet
    checks: CheckStatus = CheckStatus.NONE
    approvals: int = 0
    changes_requested: bool = False


class PullRequestQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: Literal["open", "closed", "all"] = "open"
    author: str | None = None
    limit: int = Field(default=30, ge=1, le=100)


class MergeMethod(StrEnum):
    MERGE = "merge"
    SQUASH = "squash"
    REBASE = "rebase"


class MergeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    merged: bool
    sha: str | None = None
    message: str = ""
