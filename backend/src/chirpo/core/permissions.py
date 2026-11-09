"""Who may do what (D14, D39). Every action needs a verified code host account, the chirpo role
the action asks for and, for actions on a repo, that permission on the code host itself: nobody
can do through chirpo more than the code host lets them do directly."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from chirpo.core.domain import RepoPermission, RepoRef

if TYPE_CHECKING:
    from chirpo.core.workspaces import CodeHostAccount, Member


class Role(StrEnum):
    """A member's role inside one workspace. Ranked: admin > maintainer > member."""

    ADMIN = "admin"
    MAINTAINER = "maintainer"
    MEMBER = "member"

    def at_least(self, other: "Role") -> bool:
        return _ROLE_RANK[self] >= _ROLE_RANK[other]


_ROLE_RANK = {Role.MEMBER: 0, Role.MAINTAINER: 1, Role.ADMIN: 2}


class Action(StrEnum):
    VIEW = "view"  # list or show issues and pull requests
    CREATE_ISSUE = "create_issue"
    COMMENT = "comment"
    TRIAGE_ISSUE = "triage_issue"  # assign, label, close, reopen
    MERGE_PULL_REQUEST = "merge_pull_request"
    MANAGE_REPO = "manage_repo"  # label setup, repo settings, invitations
    LINK_FIRST_REPO = "link_first_repo"  # the linker becomes the workspace's admin
    LINK_REPO = "link_repo"
    UNLINK_REPO = "unlink_repo"
    CHANGE_ROLE = "change_role"
    LIST_MEMBERS = "list_members"
    CHANGE_SETTINGS = "change_settings"


@dataclass(frozen=True, slots=True)
class Requirement:
    role: Role
    codehost: RepoPermission | None  # None: a chirpo-only action, nothing to check on the code host


REQUIREMENTS: Mapping[Action, Requirement] = {
    Action.VIEW: Requirement(Role.MEMBER, RepoPermission.READ),
    Action.CREATE_ISSUE: Requirement(Role.MEMBER, RepoPermission.READ),
    Action.COMMENT: Requirement(Role.MEMBER, RepoPermission.READ),
    Action.TRIAGE_ISSUE: Requirement(Role.MEMBER, RepoPermission.TRIAGE),
    Action.MERGE_PULL_REQUEST: Requirement(Role.MAINTAINER, RepoPermission.WRITE),
    Action.MANAGE_REPO: Requirement(Role.ADMIN, RepoPermission.ADMIN),
    Action.LINK_FIRST_REPO: Requirement(Role.MEMBER, RepoPermission.WRITE),
    Action.LINK_REPO: Requirement(Role.ADMIN, RepoPermission.WRITE),
    Action.UNLINK_REPO: Requirement(Role.ADMIN, None),
    Action.CHANGE_ROLE: Requirement(Role.ADMIN, None),
    Action.LIST_MEMBERS: Requirement(Role.MEMBER, None),
    Action.CHANGE_SETTINGS: Requirement(Role.ADMIN, None),
}


class Refusal(StrEnum):
    NOT_VERIFIED = "not_verified"  # no proven code host account (or it was deleted): run /iam
    ROLE_TOO_LOW = "role_too_low"
    NO_REPO_ACCESS = "no_repo_access"  # the code host account's permission on the repo is too low
    LAST_ADMIN = "last_admin"


@dataclass(frozen=True, slots=True)
class Verdict:
    action: Action
    refusal: Refusal | None = None
    # What the code host said, when it was asked (for messages and the audit log).
    codehost_permission: RepoPermission | None = None

    @property
    def allowed(self) -> bool:
        return self.refusal is None

    @property
    def requirement(self) -> Requirement:
        return REQUIREMENTS[self.action]


class PermissionSource(Protocol):
    """The part of CodeHostAdapter the checker needs."""

    async def current_login(self, repo: RepoRef, user_id: str) -> str | None: ...

    async def permission_of(self, repo: RepoRef, username: str) -> RepoPermission: ...


CACHE_TTL = timedelta(seconds=60)
CACHE_LIMIT = 10_000


class PermissionChecker:
    """Checks are cheapest first: verified, then role, then the code host.

    The code host is asked by the account's permanent id, never by the stored login: logins can be
    renamed and re-registered by someone else. Only granted access is cached, and only for actions
    below write (view, issues, triage) and for CACHE_TTL; powerful actions always ask afresh.
    """

    def __init__(
        self,
        codehost: PermissionSource,
        *,
        ttl: timedelta = CACHE_TTL,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._codehost = codehost
        self._ttl = ttl
        self._now = now
        self._cache: dict[tuple[str, str, str], tuple[RepoPermission, datetime]] = {}

    async def check(self, actor: "Member", action: Action, repo: RepoRef | None = None) -> Verdict:
        requirement = REQUIREMENTS[action]
        if actor.codehost is None:
            return Verdict(action, Refusal.NOT_VERIFIED)
        if not actor.role.at_least(requirement.role):
            return Verdict(action, Refusal.ROLE_TOO_LOW)
        if requirement.codehost is None:
            return Verdict(action)
        if repo is None:
            raise ValueError(f"{action} is checked against a repo")
        fresh = requirement.codehost.at_least(RepoPermission.WRITE)
        permission = await self._permission(repo, actor.codehost, fresh=fresh)
        if permission is None:
            return Verdict(action, Refusal.NOT_VERIFIED)
        if not permission.at_least(requirement.codehost):
            return Verdict(action, Refusal.NO_REPO_ACCESS, permission)
        return Verdict(action, codehost_permission=permission)

    async def _permission(
        self, repo: RepoRef, account: "CodeHostAccount", *, fresh: bool
    ) -> RepoPermission | None:
        """None when the account no longer exists."""
        key = (repo.key, account.provider, account.user_id)
        now = self._now()
        cached = self._cache.get(key)
        if not fresh and cached and cached[1] > now:
            return cached[0]
        login = await self._codehost.current_login(repo, account.user_id)
        if login is None:
            return None
        permission = await self._codehost.permission_of(repo, login)
        if permission != RepoPermission.NONE:  # a refusal is never cached: retrying must work
            if len(self._cache) >= CACHE_LIMIT:
                self._cache.clear()
            self._cache[key] = (permission, now + self._ttl)
        return permission


def role_change_refusal(target: "Member", new_role: Role, admin_count: int) -> Refusal | None:
    """A workspace always keeps at least one admin."""
    if target.role == Role.ADMIN and new_role != Role.ADMIN and admin_count <= 1:
        return Refusal.LAST_ADMIN
    return None
