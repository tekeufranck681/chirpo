import pytest
from pydantic import ValidationError

from chirpo.core.domain import (
    CloseReason,
    IssuePatch,
    IssueQuery,
    IssueState,
    LabelSpec,
    MergeMethod,
    NewIssue,
    PullRequestQuery,
    PullRequestState,
    RepoPermission,
    RepoRef,
    same_login,
)
from chirpo.core.errors import CodeHostConflictError, CodeHostNotFoundError
from tests.fakes import FakeCodeHost

REPO = RepoRef(provider="github", owner="tekeufranck681", name="chirpo-sandbox")


# ------------------------------------------------------------------ RepoRef


def test_repo_full_name() -> None:
    assert REPO.full_name == "tekeufranck681/chirpo-sandbox"


@pytest.mark.parametrize(
    "text", ["tekeufranck681/chirpo-sandbox", " tekeufranck681/chirpo-sandbox ", "a/b.c_d-e"]
)
def test_parse_repo(text: str) -> None:
    repo = RepoRef.parse("github", text)
    assert repo.full_name == text.strip()


@pytest.mark.parametrize(
    "text",
    ["", "noslash", "a/b/c", "-bad/repo", "own er/repo", "owner/", "/repo", "o/" + "x" * 101],
)
def test_parse_rejects_invalid_repo(text: str) -> None:
    with pytest.raises(ValueError, match="owner/repo"):
        RepoRef.parse("github", text)


# ------------------------------------------------------------------ issues


def test_new_issue_title_is_trimmed() -> None:
    assert NewIssue(title="  Checkout crashes  ").title == "Checkout crashes"


@pytest.mark.parametrize("title", ["", "   ", "x" * 257])
def test_new_issue_title_must_be_reasonable(title: str) -> None:
    with pytest.raises(ValidationError):
        NewIssue(title=title)


def test_empty_patch_is_detected() -> None:
    assert IssuePatch().is_empty
    assert not IssuePatch(state=IssueState.CLOSED).is_empty


@pytest.mark.parametrize("limit", [0, 101])
def test_query_limit_is_bounded(limit: int) -> None:
    with pytest.raises(ValidationError):
        IssueQuery(limit=limit)


# ------------------------------------------------------------------ permissions


def test_permissions_are_ranked() -> None:
    assert RepoPermission.ADMIN.at_least(RepoPermission.WRITE)
    assert RepoPermission.WRITE.at_least(RepoPermission.WRITE)
    assert not RepoPermission.TRIAGE.at_least(RepoPermission.WRITE)
    assert not RepoPermission.NONE.at_least(RepoPermission.READ)


# ------------------------------------------------------------------ labels


@pytest.mark.parametrize("color", ["#ff0000", "red", "ff00", "gggggg"])
def test_label_color_is_six_hex_digits(color: str) -> None:
    with pytest.raises(ValidationError):
        LabelSpec(name="status: todo", color=color)


# ------------------------------------------------------------------ fake code host behaviour


async def test_fake_creates_updates_and_lists_issues() -> None:
    host = FakeCodeHost()
    created = await host.create_issue(REPO, NewIssue(title="Checkout crashes", labels=("bug",)))
    assert (created.number, created.state) == (1, IssueState.OPEN)

    closed = await host.update_issue(
        REPO, 1, IssuePatch(state=IssueState.CLOSED, assignees=("franck",))
    )
    assert closed.state == IssueState.CLOSED
    assert closed.assignees == ("franck",)

    assert await host.list_issues(REPO, IssueQuery()) == []
    assert await host.list_issues(REPO, IssueQuery(state="all")) == [closed]


async def test_fake_unknown_issue_is_not_found() -> None:
    with pytest.raises(CodeHostNotFoundError):
        await FakeCodeHost().get_issue(REPO, 99)


async def test_fake_ensure_labels_is_idempotent() -> None:
    host = FakeCodeHost()
    labels = [LabelSpec(name="status: todo", color="ededed")]
    await host.ensure_labels(REPO, labels)
    await host.ensure_labels(REPO, labels)
    assert host.labels[REPO.key] == {"status: todo"}


# ------------------------------------------------------------------ review follow-ups


def test_repo_identity_is_case_insensitive() -> None:
    assert RepoRef.parse("github", "TekeuFranck681/Chirpo-Sandbox").key == REPO.key


def test_enterprise_owner_with_underscore_is_valid() -> None:
    assert RepoRef.parse("github", "octo_corp/app").owner == "octo_corp"


def test_same_login_ignores_case() -> None:
    assert same_login("Franck", "franck")
    assert not same_login("franck", "frank")


@pytest.mark.parametrize("field", ["assignees", "labels"])
def test_empty_names_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        NewIssue(title="x", **{field: ("ok", " ")})


async def test_update_replaces_lists_validates_and_bumps_updated_at() -> None:
    host = FakeCodeHost()
    created = await host.create_issue(REPO, NewIssue(title="x", labels=("bug", "ui")))
    updated = await host.update_issue(REPO, created.number, IssuePatch(labels=("p1",)))
    assert updated.labels == ("p1",)
    assert updated.updated_at > created.updated_at


async def test_close_with_reason_and_reopen_clears_it() -> None:
    host = FakeCodeHost()
    await host.create_issue(REPO, NewIssue(title="x"))
    closed = await host.update_issue(
        REPO, 1, IssuePatch(state=IssueState.CLOSED, close_reason=CloseReason.DUPLICATE)
    )
    assert closed.close_reason == CloseReason.DUPLICATE
    reopened = await host.update_issue(REPO, 1, IssuePatch(state=IssueState.OPEN))
    assert reopened.close_reason is None


async def test_issues_and_pull_requests_share_numbers() -> None:
    host = FakeCodeHost()
    first = await host.create_issue(REPO, NewIssue(title="issue"))
    pull = host.add_pull_request(REPO)
    second = await host.create_issue(REPO, NewIssue(title="issue 2"))
    assert (first.number, pull.number, second.number) == (1, 2, 3)
    with pytest.raises(CodeHostNotFoundError):
        await host.get_issue(REPO, pull.number)


async def test_assignee_filter_ignores_case() -> None:
    host = FakeCodeHost()
    await host.create_issue(REPO, NewIssue(title="x", assignees=("Franck",)))
    assert len(await host.list_issues(REPO, IssueQuery(assignee="franck"))) == 1


async def test_pull_request_filters() -> None:
    host = FakeCodeHost()
    host.add_pull_request(REPO, author="franck")
    host.add_pull_request(REPO, author="kevin", state=PullRequestState.CLOSED)
    assert len(await host.list_pull_requests(REPO, PullRequestQuery())) == 1
    assert len(await host.list_pull_requests(REPO, PullRequestQuery(state="closed"))) == 1
    query = PullRequestQuery(state="all", author="KEVIN")
    assert len(await host.list_pull_requests(REPO, query)) == 1


@pytest.mark.parametrize(
    "fields",
    [{"draft": True}, {"mergeable": False}, {"state": PullRequestState.CLOSED}],
)
async def test_unmergeable_pull_request_raises_conflict(fields: dict[str, object]) -> None:
    host = FakeCodeHost()
    pull = host.add_pull_request(REPO, **fields)
    with pytest.raises(CodeHostConflictError):
        await host.merge_pull_request(REPO, pull.number, MergeMethod.SQUASH)


async def test_mergeable_pull_request_merges() -> None:
    host = FakeCodeHost()
    pull = host.add_pull_request(REPO)
    result = await host.merge_pull_request(REPO, pull.number, MergeMethod.SQUASH)
    assert result.merged
    assert (await host.get_pull_request(REPO, pull.number)).state == PullRequestState.MERGED
