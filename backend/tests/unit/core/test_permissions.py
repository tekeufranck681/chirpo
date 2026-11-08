import itertools
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from chirpo.core.domain import RepoPermission, RepoRef
from chirpo.core.permissions import (
    REQUIREMENTS,
    Action,
    PermissionChecker,
    Refusal,
    Role,
    role_change_refusal,
)
from chirpo.core.workspaces import CodeHostAccount, Member

SANDBOX = RepoRef.parse("github", "tekeufranck681/chirpo-sandbox")
OTHER = RepoRef.parse("github", "acme/shop-app")
WORKSPACE = uuid.uuid4()


class FakeGitHub:
    """Logins by account id and permissions by (repo, login), like GitHub; counts lookups."""

    def __init__(self, permissions: dict[tuple[str, str], RepoPermission] | None = None) -> None:
        self.permissions = permissions or {}
        self.logins = {"42": "kevin-gh"}
        self.calls = 0
        self.fail = False

    async def current_login(self, repo: RepoRef, user_id: str) -> str | None:
        return self.logins.get(user_id)

    async def permission_of(self, repo: RepoRef, username: str) -> RepoPermission:
        self.calls += 1
        if self.fail:
            raise RuntimeError("GitHub is down")
        return self.permissions.get((repo.key, username.casefold()), RepoPermission.NONE)


def member(role: Role = Role.MEMBER, login: str | None = "kevin-gh", user_id: str = "42") -> Member:
    account = CodeHostAccount(provider="github", user_id=user_id, login=login) if login else None
    return Member(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE,
        display_name="Kevin",
        role=role,
        codehost=account,
    )


def github_with(permission: RepoPermission, login: str = "kevin-gh") -> FakeGitHub:
    return FakeGitHub({(SANDBOX.key, login): permission})


# ------------------------------------------------------------------ roles


def test_roles_are_ranked() -> None:
    assert Role.ADMIN.at_least(Role.MAINTAINER)
    assert Role.MAINTAINER.at_least(Role.MAINTAINER)
    assert not Role.MEMBER.at_least(Role.MAINTAINER)


def test_every_action_has_a_requirement() -> None:
    assert set(REQUIREMENTS) == set(Action)


# ------------------------------------------------------------------ the full table


@pytest.mark.parametrize(
    ("action", "role", "permission"),
    list(itertools.product(Action, Role, RepoPermission)),
)
async def test_allowed_exactly_when_role_and_github_permission_are_enough(
    action: Action, role: Role, permission: RepoPermission
) -> None:
    requirement = REQUIREMENTS[action]
    verdict = await PermissionChecker(github_with(permission)).check(member(role), action, SANDBOX)

    role_ok = role.at_least(requirement.role)
    github_ok = requirement.codehost is None or permission.at_least(requirement.codehost)
    assert verdict.allowed is (role_ok and github_ok)
    if not role_ok:
        assert verdict.refusal == Refusal.ROLE_TOO_LOW
    elif not github_ok:
        assert (verdict.refusal, verdict.codehost_permission) == (
            Refusal.NO_REPO_ACCESS,
            permission,
        )


@pytest.mark.parametrize("action", list(Action))
async def test_unverified_people_can_do_nothing(action: Action) -> None:
    github = github_with(RepoPermission.ADMIN)
    verdict = await PermissionChecker(github).check(member(Role.ADMIN, login=None), action, SANDBOX)
    assert verdict.refusal == Refusal.NOT_VERIFIED
    assert github.calls == 0


# ------------------------------------------------------------------ the agreed examples (D39)


async def test_a_collaborator_with_read_can_create_issues() -> None:
    verdict = await PermissionChecker(github_with(RepoPermission.READ)).check(
        member(), Action.CREATE_ISSUE, SANDBOX
    )
    assert verdict.allowed
    assert verdict.codehost_permission == RepoPermission.READ


async def test_someone_without_repo_access_cannot_create_issues() -> None:
    verdict = await PermissionChecker(FakeGitHub()).check(member(), Action.CREATE_ISSUE, SANDBOX)
    assert verdict.refusal == Refusal.NO_REPO_ACCESS


async def test_a_chirpo_admin_cannot_exceed_their_github_rights() -> None:
    verdict = await PermissionChecker(github_with(RepoPermission.TRIAGE)).check(
        member(Role.ADMIN), Action.MERGE_PULL_REQUEST, SANDBOX
    )
    assert verdict.refusal == Refusal.NO_REPO_ACCESS


async def test_github_write_alone_does_not_make_a_maintainer() -> None:
    verdict = await PermissionChecker(github_with(RepoPermission.WRITE)).check(
        member(Role.MEMBER), Action.MERGE_PULL_REQUEST, SANDBOX
    )
    assert verdict.refusal == Refusal.ROLE_TOO_LOW


async def test_the_first_link_needs_github_write_but_no_chirpo_role() -> None:
    checker = PermissionChecker(github_with(RepoPermission.WRITE))
    assert (await checker.check(member(), Action.LINK_FIRST_REPO, SANDBOX)).allowed
    assert not (await checker.check(member(), Action.LINK_REPO, SANDBOX)).allowed


async def test_chirpo_only_actions_never_ask_github() -> None:
    github = FakeGitHub()
    verdict = await PermissionChecker(github).check(member(Role.ADMIN), Action.CHANGE_ROLE)
    assert verdict.allowed
    assert github.calls == 0


async def test_repo_actions_need_a_repo() -> None:
    with pytest.raises(ValueError, match="checked against a repo"):
        await PermissionChecker(FakeGitHub()).check(member(), Action.CREATE_ISSUE)


async def test_the_role_is_checked_before_asking_github() -> None:
    github = FakeGitHub()
    await PermissionChecker(github).check(member(), Action.MANAGE_REPO, SANDBOX)
    assert github.calls == 0


# ------------------------------------------------------------------ cache


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 9, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


async def test_granted_access_is_cached_briefly_per_repo_and_account() -> None:
    github = FakeGitHub(
        {
            (SANDBOX.key, "kevin-gh"): RepoPermission.WRITE,
            (OTHER.key, "kevin-gh"): RepoPermission.READ,
        }
    )
    clock = Clock()
    checker = PermissionChecker(github, ttl=timedelta(seconds=60), now=clock)
    kevin = member()

    await checker.check(kevin, Action.CREATE_ISSUE, SANDBOX)
    await checker.check(kevin, Action.COMMENT, SANDBOX)
    await checker.check(member(login="stored-old-login"), Action.VIEW, SANDBOX)  # same account
    assert github.calls == 1

    await checker.check(kevin, Action.CREATE_ISSUE, OTHER)
    assert github.calls == 2

    github.permissions[(SANDBOX.key, "kevin-gh")] = RepoPermission.NONE  # access removed on GitHub
    clock.now += timedelta(seconds=61)
    verdict = await checker.check(kevin, Action.CREATE_ISSUE, SANDBOX)
    assert verdict.refusal == Refusal.NO_REPO_ACCESS
    assert github.calls == 3


async def test_a_refusal_is_not_cached() -> None:
    github = FakeGitHub()
    checker = PermissionChecker(github)
    assert not (await checker.check(member(), Action.CREATE_ISSUE, SANDBOX)).allowed
    github.permissions[(SANDBOX.key, "kevin-gh")] = RepoPermission.READ  # added as collaborator
    assert (await checker.check(member(), Action.CREATE_ISSUE, SANDBOX)).allowed


async def test_a_failed_lookup_is_not_cached() -> None:
    github = github_with(RepoPermission.READ)
    checker = PermissionChecker(github)
    github.fail = True
    with pytest.raises(RuntimeError):
        await checker.check(member(), Action.CREATE_ISSUE, SANDBOX)
    github.fail = False
    assert (await checker.check(member(), Action.CREATE_ISSUE, SANDBOX)).allowed


@pytest.mark.parametrize(
    "action", [Action.MERGE_PULL_REQUEST, Action.LINK_FIRST_REPO, Action.MANAGE_REPO]
)
async def test_powerful_actions_always_ask_github(action: Action) -> None:
    github = github_with(RepoPermission.ADMIN)
    checker = PermissionChecker(github)
    for _ in range(2):
        await checker.check(member(Role.ADMIN), action, SANDBOX)
    assert github.calls == 2


async def test_a_renamed_login_taken_by_someone_else_grants_nothing() -> None:
    # Alice verified as "alice" (account 7), renamed to "alice2"; a stranger took "alice".
    github = FakeGitHub(
        {
            (SANDBOX.key, "alice"): RepoPermission.ADMIN,  # the stranger's own repo access
            (SANDBOX.key, "alice2"): RepoPermission.READ,
        }
    )
    github.logins = {"7": "alice2"}
    alice = member(Role.ADMIN, login="alice", user_id="7")

    checker = PermissionChecker(github)
    assert (await checker.check(alice, Action.CREATE_ISSUE, SANDBOX)).allowed
    verdict = await checker.check(alice, Action.MERGE_PULL_REQUEST, SANDBOX)
    assert (verdict.refusal, verdict.codehost_permission) == (
        Refusal.NO_REPO_ACCESS,
        RepoPermission.READ,
    )


async def test_a_deleted_account_must_verify_again() -> None:
    github = github_with(RepoPermission.ADMIN)
    github.logins = {}
    verdict = await PermissionChecker(github).check(member(), Action.CREATE_ISSUE, SANDBOX)
    assert verdict.refusal == Refusal.NOT_VERIFIED


async def test_maintain_satisfies_write() -> None:
    checker = PermissionChecker(github_with(RepoPermission.MAINTAIN))
    verdict = await checker.check(member(Role.MAINTAINER), Action.MERGE_PULL_REQUEST, SANDBOX)
    assert verdict.allowed


# ------------------------------------------------------------------ role changes


@pytest.mark.parametrize(
    ("current", "new", "admins", "refused"),
    [
        (Role.ADMIN, Role.MEMBER, 1, True),
        (Role.ADMIN, Role.MAINTAINER, 1, True),
        (Role.ADMIN, Role.MEMBER, 2, False),
        (Role.ADMIN, Role.ADMIN, 1, False),
        (Role.MEMBER, Role.ADMIN, 1, False),
        (Role.MAINTAINER, Role.MEMBER, 1, False),
    ],
)
def test_a_workspace_keeps_at_least_one_admin(
    current: Role, new: Role, admins: int, refused: bool
) -> None:
    refusal = role_change_refusal(member(current), new, admins)
    assert (refusal == Refusal.LAST_ADMIN) is refused
