"""GitHubAdapter (issues, labels, users) against saved GitHub payloads and a mocked API."""

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from githubkit import AppAuthStrategy

from chirpo.adapters.codehost.github.adapter import MAX_ISSUE_PAGES, PAGE_SIZE, GitHubAdapter
from chirpo.adapters.codehost.github.auth import GitHubAuth
from chirpo.core.domain import (
    CloseReason,
    IssuePatch,
    IssueQuery,
    IssueState,
    LabelSpec,
    NewIssue,
    RepoPermission,
    RepoRef,
)
from chirpo.core.errors import (
    CodeHostForbiddenError,
    CodeHostNotFoundError,
    CodeHostValidationError,
)
from tests.integration.github_api import mocked_github, payload

REPO = RepoRef(provider="github", owner="tekeufranck681", name="chirpo")
ISSUES = "/repos/tekeufranck681/chirpo/issues"
LABELS = "/repos/tekeufranck681/chirpo/labels"


@pytest.fixture
def github() -> Iterator[respx.MockRouter]:
    with mocked_github() as router:
        yield router


@pytest.fixture
def adapter() -> GitHubAdapter:
    return GitHubAdapter(GitHubAuth(token="ghp_devtoken", auto_retry=False))


def sent(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


def params(route: respx.Route) -> dict[str, str]:
    return dict(route.calls.last.request.url.params)


# ------------------------------------------------------------------ issues


async def test_create_issue(adapter: GitHubAdapter, github: respx.MockRouter) -> None:
    route = github.post(ISSUES).respond(201, json=payload("issue"))

    issue = await adapter.create_issue(
        REPO,
        NewIssue(
            title="Checkout crashes on Android 13",
            body="Tapping Pay closes the app.",
            assignees=("tekeufranck681",),
            labels=("bug",),
        ),
    )

    assert sent(route) == {
        "title": "Checkout crashes on Android 13",
        "body": "Tapping Pay closes the app.",
        "assignees": ["tekeufranck681"],
        "labels": ["bug"],
    }
    assert (issue.number, issue.state, issue.close_reason) == (12, IssueState.OPEN, None)
    assert (issue.assignees, issue.labels, issue.author) == (
        ("tekeufranck681",),
        ("bug",),
        "tekeufranck681",
    )
    assert issue.url == "https://github.com/tekeufranck681/chirpo/issues/12"
    assert issue.repo == REPO


async def test_unknown_assignee_is_a_validation_error(
    adapter: GitHubAdapter, github: respx.MockRouter
) -> None:
    github.post(ISSUES).respond(422, json={"message": "Validation Failed"})
    with pytest.raises(CodeHostValidationError):
        await adapter.create_issue(REPO, NewIssue(title="x", assignees=("nobody-here",)))


async def test_get_issue(adapter: GitHubAdapter, github: respx.MockRouter) -> None:
    github.get(f"{ISSUES}/12").respond(json=payload("issue"))
    assert (await adapter.get_issue(REPO, 12)).title == "Checkout crashes on Android 13"


async def test_get_issue_on_a_pull_request_is_not_found(
    adapter: GitHubAdapter, github: respx.MockRouter
) -> None:
    pull_request = payload("issues_page")[1]
    github.get(f"{ISSUES}/11").respond(json=pull_request)
    with pytest.raises(CodeHostNotFoundError, match="pull request"):
        await adapter.get_issue(REPO, 11)


async def test_close_as_not_planned(adapter: GitHubAdapter, github: respx.MockRouter) -> None:
    route = github.patch(f"{ISSUES}/12").respond(json=payload("issue_closed"))

    issue = await adapter.update_issue(
        REPO, 12, IssuePatch(state=IssueState.CLOSED, close_reason=CloseReason.NOT_PLANNED)
    )

    assert sent(route) == {"state": "closed", "state_reason": "not_planned"}
    assert (issue.state, issue.close_reason) == (IssueState.CLOSED, CloseReason.NOT_PLANNED)


async def test_reopen_clears_the_close_reason(
    adapter: GitHubAdapter, github: respx.MockRouter
) -> None:
    route = github.patch(f"{ISSUES}/12").respond(json=payload("issue"))
    issue = await adapter.update_issue(REPO, 12, IssuePatch(state=IssueState.OPEN))
    assert sent(route) == {"state": "open"}
    assert issue.close_reason is None


async def test_empty_lists_clear_labels_and_assignees(
    adapter: GitHubAdapter, github: respx.MockRouter
) -> None:
    route = github.patch(f"{ISSUES}/12").respond(json=payload("issue"))
    await adapter.update_issue(REPO, 12, IssuePatch(labels=(), assignees=()))
    assert sent(route) == {"labels": [], "assignees": []}


async def test_empty_patch_only_reads(adapter: GitHubAdapter, github: respx.MockRouter) -> None:
    patch = github.patch(f"{ISSUES}/12")
    github.get(f"{ISSUES}/12").respond(json=payload("issue"))
    await adapter.update_issue(REPO, 12, IssuePatch())
    assert not patch.called


async def test_list_issues_drops_pull_requests_and_sends_filters(
    adapter: GitHubAdapter, github: respx.MockRouter
) -> None:
    route = github.get(ISSUES).respond(json=payload("issues_page"))

    issues = await adapter.list_issues(
        REPO, IssueQuery(state="all", assignee="tekeufranck681", labels=("bug", "p1"))
    )

    assert [issue.number for issue in issues] == [12]
    assert params(route) == {
        "state": "all",
        "assignee": "tekeufranck681",
        "labels": "bug,p1",
        "per_page": str(PAGE_SIZE),
        "page": "1",
    }


async def test_list_issues_reads_more_pages_when_pull_requests_fill_one(
    adapter: GitHubAdapter, github: respx.MockRouter
) -> None:
    pull_request = payload("issues_page")[1]
    route = github.get(ISSUES).mock(
        side_effect=[
            httpx.Response(200, json=[pull_request] * PAGE_SIZE),
            httpx.Response(200, json=payload("issues_page")),
        ]
    )
    issues = await adapter.list_issues(REPO, IssueQuery())
    assert [issue.number for issue in issues] == [12]
    assert route.call_count == 2


async def test_list_issues_stops_at_the_limit(
    adapter: GitHubAdapter, github: respx.MockRouter
) -> None:
    route = github.get(ISSUES).respond(json=[payload("issue")] * PAGE_SIZE)
    assert len(await adapter.list_issues(REPO, IssueQuery(limit=3))) == 3
    assert route.call_count == 1


async def test_add_comment_returns_its_link(
    adapter: GitHubAdapter, github: respx.MockRouter
) -> None:
    route = github.post(f"{ISSUES}/12/comments").respond(201, json=payload("comment"))
    url = await adapter.add_comment(REPO, 12, "Repro on a Pixel 7 too.")
    assert sent(route) == {"body": "Repro on a Pixel 7 too."}
    assert url.endswith("/issues/12#issuecomment-3500000001")


# ------------------------------------------------------------------ labels


async def test_ensure_labels_creates_only_missing_ones(
    adapter: GitHubAdapter, github: respx.MockRouter
) -> None:
    github.get(LABELS).respond(json=payload("labels"))
    create = github.post(LABELS).respond(201, json=payload("label_created"))

    await adapter.ensure_labels(
        REPO,
        [
            LabelSpec(name="BUG", color="d73a4a"),
            LabelSpec(name="status: todo", color="ededed", description="Not started"),
        ],
    )

    assert create.call_count == 1
    assert sent(create) == {"name": "status: todo", "color": "ededed", "description": "Not started"}


async def test_label_created_meanwhile_is_fine(
    adapter: GitHubAdapter, github: respx.MockRouter
) -> None:
    github.get(LABELS).mock(
        side_effect=[
            httpx.Response(200, json=payload("labels")),
            httpx.Response(200, json=[*payload("labels"), payload("label_created")]),
        ]
    )
    github.post(LABELS).respond(422, json={"message": "Validation Failed"})
    await adapter.ensure_labels(REPO, [LabelSpec(name="status: todo", color="ededed")])


async def test_invalid_label_is_reported(adapter: GitHubAdapter, github: respx.MockRouter) -> None:
    github.get(LABELS).respond(json=payload("labels"))
    github.post(LABELS).respond(422, json={"message": "Validation Failed"})
    with pytest.raises(CodeHostValidationError):
        await adapter.ensure_labels(REPO, [LabelSpec(name="status: todo", color="ededed")])


# ------------------------------------------------------------------ users and permissions


async def test_user_exists(adapter: GitHubAdapter, github: respx.MockRouter) -> None:
    github.get("/users/tekeufranck681").respond(json=payload("user"))
    github.get("/users/nobody-here").respond(404, json={"message": "Not Found"})
    assert await adapter.user_exists(REPO, "tekeufranck681")
    assert not await adapter.user_exists(REPO, "nobody-here")


@pytest.mark.parametrize(
    ("role_name", "permission", "expected"),
    [
        ("admin", "admin", RepoPermission.ADMIN),
        ("triage", "read", RepoPermission.TRIAGE),
        ("maintain", "write", RepoPermission.MAINTAIN),
        ("security-manager", "write", RepoPermission.WRITE),  # custom org role
    ],
)
async def test_permission_of(
    adapter: GitHubAdapter,
    github: respx.MockRouter,
    role_name: str,
    permission: str,
    expected: RepoPermission,
) -> None:
    body = {**payload("collaborator_permission"), "role_name": role_name, "permission": permission}
    github.get("/repos/tekeufranck681/chirpo/collaborators/kevin/permission").respond(json=body)
    assert await adapter.permission_of(REPO, "kevin") == expected


async def test_non_collaborator_has_no_permission(
    adapter: GitHubAdapter, github: respx.MockRouter
) -> None:
    github.get("/repos/tekeufranck681/chirpo/collaborators/kevin/permission").respond(
        404, json={"message": "Not Found"}
    )
    assert await adapter.permission_of(REPO, "kevin") == RepoPermission.NONE


# ------------------------------------------------------------------ App installation


@pytest.fixture
def app_adapter() -> GitHubAdapter:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    return GitHubAdapter(GitHubAuth(app=AppAuthStrategy(1234567, pem), auto_retry=False))


INSTALLATION = "/repos/tekeufranck681/chirpo/installation"
TOKENS = "/app/installations/87654321/access_tokens"


async def test_installation_and_token_are_reused_across_calls(
    app_adapter: GitHubAdapter, github: respx.MockRouter
) -> None:
    lookup = github.get(INSTALLATION).respond(json=payload("installation"))
    mint = github.post(TOKENS).respond(201, json=payload("access_token"))
    github.get(f"{ISSUES}/12").respond(json=payload("issue"))
    github.get(f"{ISSUES}/99").respond(404, json={"message": "Not Found"})

    await app_adapter.get_issue(REPO, 12)
    with pytest.raises(CodeHostNotFoundError):
        await app_adapter.get_issue(REPO, 99)  # a missing issue is not an installation problem
    await app_adapter.get_issue(REPO, 12)

    assert (lookup.call_count, mint.call_count) == (1, 1)


async def test_lost_access_forgets_the_installation(
    app_adapter: GitHubAdapter, github: respx.MockRouter
) -> None:
    lookup = github.get(INSTALLATION).respond(json=payload("installation"))
    github.post(TOKENS).respond(404, json={"message": "Not Found"})

    for _ in range(2):
        with pytest.raises(CodeHostForbiddenError, match="lost access"):
            await app_adapter.get_issue(REPO, 12)

    assert lookup.call_count == 2


async def test_permission_of_on_an_unreachable_repo_is_not_hidden(
    app_adapter: GitHubAdapter, github: respx.MockRouter
) -> None:
    github.get(INSTALLATION).respond(404, json={"message": "Not Found"})
    with pytest.raises(CodeHostForbiddenError, match="not installed"):
        await app_adapter.permission_of(REPO, "kevin")


async def test_dropped_assignees_show_in_the_result(
    adapter: GitHubAdapter, github: respx.MockRouter
) -> None:
    github.post(ISSUES).respond(201, json={**payload("issue"), "assignee": None, "assignees": []})
    issue = await adapter.create_issue(REPO, NewIssue(title="x", assignees=("kevin",)))
    assert issue.assignees == ()


@pytest.mark.parametrize("username", ["../repos/x", "a/b", "", "-dash", "x" * 40])
async def test_invalid_usernames_never_reach_github(
    adapter: GitHubAdapter, github: respx.MockRouter, username: str
) -> None:
    assert not await adapter.user_exists(REPO, username)
    assert await adapter.permission_of(REPO, username) == RepoPermission.NONE
    assert not github.calls


async def test_issue_list_gives_up_after_the_page_cap(
    adapter: GitHubAdapter, github: respx.MockRouter
) -> None:
    pull_request = payload("issues_page")[1]
    route = github.get(ISSUES).respond(json=[pull_request] * PAGE_SIZE)
    assert await adapter.list_issues(REPO, IssueQuery()) == []
    assert route.call_count == MAX_ISSUE_PAGES
