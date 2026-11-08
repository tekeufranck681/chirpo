"""GitHub CodeHostAdapter: REST for issues and writes, GraphQL to read pull requests."""

import re
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal

from githubkit import GitHub, Response
from githubkit.utils import UNSET
from githubkit_schemas.latest.models import Issue as GitHubIssue
from githubkit_schemas.latest.models import Label
from githubkit_schemas.latest.models import (
    ReposOwnerRepoIssuesIssueNumberPatchResponse200 as GitHubUpdatedIssue,
)

from chirpo.adapters.codehost.github.auth import GitHubAuth
from chirpo.adapters.codehost.github.errors import translate_errors
from chirpo.adapters.codehost.github.queries import GET_PULL_REQUEST, LIST_PULL_REQUESTS
from chirpo.core.domain import (
    CheckStatus,
    CloseReason,
    Issue,
    IssuePatch,
    IssueQuery,
    IssueState,
    LabelSpec,
    MergeMethod,
    MergeResult,
    NewIssue,
    PullRequest,
    PullRequestQuery,
    PullRequestState,
    RepoPermission,
    RepoRef,
    same_login,
)
from chirpo.core.errors import (
    CodeHostForbiddenError,
    CodeHostNotFoundError,
    CodeHostValidationError,
)
from chirpo.logging import get_logger

PAGE_SIZE = 100
# GitHub's issue list mixes in pull requests, so a page can hold fewer issues than asked for.
MAX_ISSUE_PAGES = 5
MAX_LABEL_PAGES = 10
MAX_PULL_REQUEST_PAGES = 5
_CLOSE_REASONS = {reason.value for reason in CloseReason}
_PERMISSIONS = {permission.value for permission in RepoPermission}
# GitHub logins: letters, digits and single hyphens, max 39 characters; Apps end in [bot].
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?")

log = get_logger(__name__)

# GitHub's combined result of checks and commit statuses on the PR's latest commit.
_CHECKS = {
    "SUCCESS": CheckStatus.PASSING,
    "FAILURE": CheckStatus.FAILING,
    "ERROR": CheckStatus.FAILING,
    "PENDING": CheckStatus.PENDING,
    "EXPECTED": CheckStatus.PENDING,
}
_MERGEABLE = {"MERGEABLE": True, "CONFLICTING": False}  # UNKNOWN: GitHub is still computing it
_PULL_REQUEST_STATES: dict[str, list[str] | None] = {
    "open": ["OPEN"],
    "closed": ["CLOSED", "MERGED"],
    "all": None,
}


def _to_issue(repo: RepoRef, found: GitHubIssue | GitHubUpdatedIssue) -> Issue:
    reason = found.state_reason if found.state == "closed" else None
    return Issue(
        repo=repo,
        number=found.number,
        title=found.title,
        body=found.body or "",
        state=IssueState(found.state),
        close_reason=CloseReason(reason) if reason in _CLOSE_REASONS else None,
        assignees=tuple(user.login for user in found.assignees or ()),
        labels=tuple(
            label if isinstance(label, str) else label.name or "" for label in found.labels
        ),
        url=found.html_url,
        author=found.user.login if found.user else None,
        created_at=found.created_at,
        updated_at=found.updated_at,
    )


def _to_pull_request(repo: RepoRef, node: dict[str, Any]) -> PullRequest:
    commits = node["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    checks = CheckStatus.NONE
    if rollup:
        checks = _CHECKS.get(rollup["state"], CheckStatus.NONE)
        if rollup["state"] not in _CHECKS:
            log.warning("unknown_check_state", state=rollup["state"])
    # Each reviewer's latest decision, possibly on an older commit: GitHub's own approval count.
    reviews = [review["state"] for review in node["latestOpinionatedReviews"]["nodes"]]
    author = node["author"]
    return PullRequest(
        repo=repo,
        number=node["number"],
        title=node["title"],
        state=PullRequestState(node["state"].lower()),
        # GraphQL gives bots as "dependabot"; REST and assignees use "dependabot[bot]".
        author=(f"{author['login']}[bot]" if author["__typename"] == "Bot" else author["login"])
        if author
        else None,
        head_branch=node["headRefName"],
        base_branch=node["baseRefName"],
        url=node["url"],
        draft=node["isDraft"],
        mergeable=_MERGEABLE.get(node["mergeable"]),
        checks=checks,
        closes_issues=tuple(
            issue["number"]
            for issue in node["closingIssuesReferences"]["nodes"]
            if issue["repository"]["nameWithOwner"].casefold() == repo.full_name.casefold()
        ),
        approvals=reviews.count("APPROVED"),
        changes_requested="CHANGES_REQUESTED" in reviews,
    )


def _is_pull_request(found: GitHubIssue | GitHubUpdatedIssue) -> bool:
    return found.pull_request not in (None, UNSET)


class GitHubAdapter:
    provider = "github"

    def __init__(self, auth: GitHubAuth) -> None:
        self._auth = auth

    async def _call[T](
        self,
        repo: RepoRef,
        what: str,
        request: Callable[[GitHub[Any]], Awaitable[Response[T, Any]]],
    ) -> T:
        return (await self._run(repo, what, request)).parsed_data

    async def _query(self, repo: RepoRef, what: str, query: str, **variables: Any) -> Any:
        variables |= {"owner": repo.owner, "name": repo.name}
        data = await self._run(repo, what, lambda gh: gh.async_graphql(query, variables))
        if not data.get("repository"):
            raise CodeHostNotFoundError(f"{what}: GitHub returned no repository")
        return data["repository"]

    async def _run[T](
        self, repo: RepoRef, what: str, request: Callable[[GitHub[Any]], Awaitable[T]]
    ) -> T:
        try:
            github = await self._auth.for_repo(repo)
            with translate_errors(what):
                return await request(github)
        except CodeHostForbiddenError:
            # Maybe the App was reinstalled (new installation id): look it up again next time.
            self._auth.forget(repo)
            raise

    async def create_issue(self, repo: RepoRef, issue: NewIssue) -> Issue:
        created = await self._call(
            repo,
            f"create an issue in {repo.full_name}",
            lambda gh: gh.rest.issues.async_create(
                repo.owner,
                repo.name,
                title=issue.title,
                body=issue.body,
                assignees=list(issue.assignees),
                labels=list(issue.labels),
            ),
        )
        return _to_issue(repo, created)

    async def get_issue(self, repo: RepoRef, number: int) -> Issue:
        found = await self._call(
            repo,
            f"get {repo.full_name}#{number}",
            lambda gh: gh.rest.issues.async_get(repo.owner, repo.name, number),
        )
        if _is_pull_request(found):
            raise CodeHostNotFoundError(f"{repo.full_name}#{number} is a pull request")
        return _to_issue(repo, found)

    async def update_issue(self, repo: RepoRef, number: int, patch: IssuePatch) -> Issue:
        if patch.is_empty:
            return await self.get_issue(repo, number)
        changes: dict[str, Any] = {
            "title": patch.title,
            "body": patch.body,
            "state": patch.state.value if patch.state else None,
            "state_reason": patch.close_reason.value if patch.close_reason else None,
            "assignees": list(patch.assignees) if patch.assignees is not None else None,
            "labels": list(patch.labels) if patch.labels is not None else None,
        }
        updated = await self._call(
            repo,
            f"update {repo.full_name}#{number}",
            lambda gh: gh.rest.issues.async_update(
                repo.owner,
                repo.name,
                number,
                **{key: value for key, value in changes.items() if value is not None},
            ),
        )
        return _to_issue(repo, updated)

    async def list_issues(self, repo: RepoRef, query: IssueQuery) -> list[Issue]:
        state: Literal["open", "closed", "all"] = (
            "all"
            if query.state == "all"
            else "open"
            if query.state == IssueState.OPEN
            else "closed"
        )
        issues: list[Issue] = []
        for page in range(1, MAX_ISSUE_PAGES + 1):

            def list_page(gh: GitHub[Any], page: int = page) -> Awaitable[Response[Any, Any]]:
                return gh.rest.issues.async_list_for_repo(
                    repo.owner,
                    repo.name,
                    state=state,
                    assignee=query.assignee or UNSET,
                    labels=",".join(query.labels) or UNSET,
                    per_page=PAGE_SIZE,
                    page=page,
                )

            found: list[GitHubIssue] = await self._call(
                repo, f"list issues in {repo.full_name}", list_page
            )
            issues += [_to_issue(repo, item) for item in found if not _is_pull_request(item)]
            if len(issues) >= query.limit or len(found) < PAGE_SIZE:
                break
        else:
            log.warning("issue_list_truncated", repo=repo.full_name, pages=MAX_ISSUE_PAGES)
        return issues[: query.limit]

    async def add_comment(self, repo: RepoRef, number: int, body: str) -> str:
        comment = await self._call(
            repo,
            f"comment on {repo.full_name}#{number}",
            lambda gh: gh.rest.issues.async_create_comment(
                repo.owner, repo.name, number, body=body
            ),
        )
        return comment.html_url

    async def ensure_labels(self, repo: RepoRef, labels: Sequence[LabelSpec]) -> None:
        existing = await self._label_names(repo)
        for label in labels:
            if label.name.casefold() in existing:
                continue

            def create(gh: GitHub[Any], label: LabelSpec = label) -> Awaitable[Response[Any, Any]]:
                return gh.rest.issues.async_create_label(
                    repo.owner,
                    repo.name,
                    name=label.name,
                    color=label.color,
                    description=label.description,
                )

            try:
                await self._call(repo, f"create label {label.name!r} in {repo.full_name}", create)
            except CodeHostValidationError:
                if label.name.casefold() not in await self._label_names(repo):
                    raise  # a real validation error, not "created meanwhile by someone else"

    async def _label_names(self, repo: RepoRef) -> set[str]:
        names: set[str] = set()
        for page in range(1, MAX_LABEL_PAGES + 1):

            def list_page(gh: GitHub[Any], page: int = page) -> Awaitable[Response[Any, Any]]:
                return gh.rest.issues.async_list_labels_for_repo(
                    repo.owner, repo.name, per_page=PAGE_SIZE, page=page
                )

            found: list[Label] = await self._call(
                repo, f"list labels in {repo.full_name}", list_page
            )
            names |= {label.name.casefold() for label in found}
            if len(found) < PAGE_SIZE:
                break
        return names

    async def user_exists(self, repo: RepoRef, username: str) -> bool:
        if not _LOGIN.fullmatch(username):
            return False
        try:
            await self._call(
                repo,
                f"look up GitHub user {username}",
                lambda gh: gh.rest.users.async_get_by_username(username),
            )
        except CodeHostNotFoundError:
            return False
        return True

    async def current_login(self, repo: RepoRef, user_id: str) -> str | None:
        if not user_id.isdigit():
            return None
        try:
            user = await self._call(
                repo,
                f"look up GitHub account {user_id}",
                lambda gh: gh.rest.users.async_get_by_id(int(user_id)),
            )
        except CodeHostNotFoundError:
            return None
        return user.login

    async def permission_of(self, repo: RepoRef, username: str) -> RepoPermission:
        if not _LOGIN.fullmatch(username):
            return RepoPermission.NONE
        try:
            found = await self._call(
                repo,
                f"get {username}'s access to {repo.full_name}",
                lambda gh: gh.rest.repos.async_get_collaborator_permission_level(
                    repo.owner, repo.name, username
                ),
            )
        except CodeHostNotFoundError:  # no such user; repo or App problems raise Forbidden
            return RepoPermission.NONE
        # role_name has the precise level (triage, maintain); permission is the legacy fallback.
        for level in (found.role_name, found.permission):
            if level in _PERMISSIONS:
                return RepoPermission(level)
        return RepoPermission.NONE

    async def list_pull_requests(self, repo: RepoRef, query: PullRequestQuery) -> list[PullRequest]:
        # GitHub can't filter PRs by author here, so with an author we read full pages and filter.
        page_size = PAGE_SIZE if query.author else query.limit
        pulls: list[PullRequest] = []
        after: str | None = None
        for _ in range(MAX_PULL_REQUEST_PAGES):
            found = (
                await self._query(
                    repo,
                    f"list pull requests in {repo.full_name}",
                    LIST_PULL_REQUESTS,
                    states=_PULL_REQUEST_STATES[query.state],
                    first=page_size,
                    after=after,
                )
            )["pullRequests"]
            pulls += [
                pull
                for pull in (_to_pull_request(repo, node) for node in found["nodes"])
                if query.author is None or same_login(query.author, pull.author or "")
            ]
            if len(pulls) >= query.limit or not found["pageInfo"]["hasNextPage"]:
                break
            after = found["pageInfo"]["endCursor"]
        else:
            log.warning("pull_request_list_truncated", repo=repo.full_name)
        return pulls[: query.limit]

    async def get_pull_request(self, repo: RepoRef, number: int) -> PullRequest:
        found = await self._query(
            repo, f"get {repo.full_name}!{number}", GET_PULL_REQUEST, number=number
        )
        if not found.get("pullRequest"):
            raise CodeHostNotFoundError(f"{repo.full_name}!{number} is not a pull request")
        return _to_pull_request(repo, found["pullRequest"])

    async def merge_pull_request(
        self, repo: RepoRef, number: int, method: MergeMethod
    ) -> MergeResult:
        merged = await self._call(
            repo,
            f"merge {repo.full_name}!{number}",
            lambda gh: gh.rest.pulls.async_merge(
                repo.owner, repo.name, number, merge_method=method.value
            ),
        )
        return MergeResult(merged=merged.merged, sha=merged.sha, message=merged.message)

    async def aclose(self) -> None:
        """Nothing to close: githubkit opens and closes an HTTP client per request."""


if TYPE_CHECKING:
    from chirpo.core.ports import CodeHostAdapter

    _matches_the_contract: type[CodeHostAdapter] = GitHubAdapter
