"""Fake adapters for testing the core without network calls."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from chirpo.core.domain import (
    ChatRef,
    ChatUser,
    CloseReason,
    InboundCallback,
    InboundEvent,
    InboundMessage,
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
    SentMessage,
    same_login,
)
from chirpo.core.errors import CodeHostConflictError, CodeHostNotFoundError

CHAT = ChatRef(platform="fake", chat_id="-100")
USER = ChatUser(platform="fake", user_id="42", display_name="Paul", username="paul")


def make_message(text: str, **overrides: Any) -> InboundMessage:
    values: dict[str, Any] = {
        "event_id": "1",
        "chat": CHAT,
        "sender": USER,
        "message_id": "10",
        "text": text,
        "sent_at": datetime(2026, 1, 1, tzinfo=UTC),
        **overrides,
    }
    return InboundMessage(**values)


def make_callback(data: str) -> InboundCallback:
    return InboundCallback(event_id="2", chat=CHAT, sender=USER, message_id="11", data=data)


class FakeMessaging:
    platform = "fake"
    event_source = "fake:bot"

    def __init__(self) -> None:
        self.sent: list[tuple[ChatRef, str, str | None]] = []
        self.edited: list[tuple[SentMessage, str]] = []

    def parse_update(self, raw: Mapping[str, Any]) -> InboundEvent | None:
        return None

    async def send_message(
        self, chat: ChatRef, text: str, *, reply_to: str | None = None
    ) -> SentMessage:
        self.sent.append((chat, text, reply_to))
        return SentMessage(chat=chat, message_id=str(100 + len(self.sent)))

    async def edit_message(self, message: SentMessage, text: str) -> None:
        self.edited.append((message, text))

    async def aclose(self) -> None:
        pass


class FakeProcessedEvents:
    def __init__(self) -> None:
        self.claimed: set[tuple[str, str]] = set()

    async def claim(self, source: str, external_id: str) -> bool:
        key = (source, external_id)
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True


class FakePollCursors:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    async def get(self, source: str, scope: str) -> str | None:
        return self.values.get((source, scope))

    async def set(self, source: str, scope: str, cursor: str) -> None:
        self.values[(source, scope)] = cursor


class FakeCodeHost:
    """In-memory code host with GitHub's semantics: shared issue/PR numbering, case-insensitive
    names, label/assignee lists replaced on update, merge conflicts raised."""

    provider = "fake"

    def __init__(self) -> None:
        self.issues: dict[tuple[str, int], Issue] = {}
        self.pulls: dict[tuple[str, int], PullRequest] = {}
        self.comments: list[tuple[str, int, str]] = []
        self.labels: dict[str, set[str]] = {}
        self.users: set[str] = {"franck"}
        self.default_branch = "main"
        self.permissions: dict[tuple[str, str], RepoPermission] = {}
        self._last_number: dict[str, int] = {}
        self._clock = datetime(2026, 1, 1, tzinfo=UTC)

    def _next_number(self, repo: RepoRef) -> int:
        self._last_number[repo.key] = self._last_number.get(repo.key, 0) + 1
        return self._last_number[repo.key]

    def _tick(self) -> datetime:
        self._clock += timedelta(minutes=1)
        return self._clock

    def _assignable(self, logins: tuple[str, ...]) -> tuple[str, ...]:
        """Like GitHub: people who can't be assigned are dropped without an error."""
        return tuple(login for login in logins if any(same_login(login, u) for u in self.users))

    def add_pull_request(self, repo: RepoRef, **fields: Any) -> PullRequest:
        number = self._next_number(repo)
        values: dict[str, Any] = {
            "repo": repo,
            "number": number,
            "title": f"PR {number}",
            "state": PullRequestState.OPEN,
            "head_branch": f"feature-{number}",
            "base_branch": "main",
            "url": f"https://fake.example/{repo.full_name}/pull/{number}",
            "mergeable": True,
            **fields,
        }
        pull = PullRequest(**values)
        self.pulls[(repo.key, number)] = pull
        return pull

    async def create_issue(self, repo: RepoRef, issue: NewIssue) -> Issue:
        number = self._next_number(repo)
        now = self._tick()
        created = Issue(
            repo=repo,
            number=number,
            title=issue.title,
            body=issue.body,
            state=IssueState.OPEN,
            assignees=self._assignable(issue.assignees),
            labels=issue.labels,
            url=f"https://fake.example/{repo.full_name}/issues/{number}",
            created_at=now,
            updated_at=now,
        )
        self.issues[(repo.key, number)] = created
        return created

    async def get_issue(self, repo: RepoRef, number: int) -> Issue:
        try:
            return self.issues[(repo.key, number)]
        except KeyError:
            raise CodeHostNotFoundError(f"{repo.full_name}#{number}") from None

    async def update_issue(self, repo: RepoRef, number: int, patch: IssuePatch) -> Issue:
        current = await self.get_issue(repo, number)
        changes = patch.model_dump(exclude_none=True)
        if "assignees" in changes:
            changes["assignees"] = self._assignable(changes["assignees"])
        if changes.get("state") == IssueState.OPEN:
            changes.setdefault("close_reason", None)
        updated = Issue.model_validate(
            {**current.model_dump(), **changes, "updated_at": self._tick()}
        )
        self.issues[(repo.key, number)] = updated
        return updated

    async def list_issues(self, repo: RepoRef, query: IssueQuery) -> list[Issue]:
        found = [
            issue
            for (key, _), issue in self.issues.items()
            if key == repo.key
            and (query.state == "all" or issue.state == query.state)
            and (
                query.assignee is None
                or any(same_login(query.assignee, who) for who in issue.assignees)
            )
            and set(query.labels) <= set(issue.labels)
        ]
        return found[: query.limit]

    async def add_comment(self, repo: RepoRef, number: int, body: str) -> str:
        await self.get_issue(repo, number)
        self.comments.append((repo.key, number, body))
        return f"https://fake.example/{repo.full_name}/issues/{number}#comment-{len(self.comments)}"

    async def ensure_labels(self, repo: RepoRef, labels: Sequence[LabelSpec]) -> None:
        self.labels.setdefault(repo.key, set()).update(label.name for label in labels)

    async def user_exists(self, repo: RepoRef, username: str) -> bool:
        return any(same_login(username, user) for user in self.users)

    async def permission_of(self, repo: RepoRef, username: str) -> RepoPermission:
        return self.permissions.get((repo.key, username.casefold()), RepoPermission.NONE)

    async def list_pull_requests(self, repo: RepoRef, query: PullRequestQuery) -> list[PullRequest]:
        def state_matches(pull: PullRequest) -> bool:
            if query.state == "all":
                return True
            is_open = pull.state == PullRequestState.OPEN
            return is_open if query.state == "open" else not is_open

        found = [
            pull
            for (key, _), pull in self.pulls.items()
            if key == repo.key
            and state_matches(pull)
            and (query.author is None or same_login(query.author, pull.author or ""))
        ]
        return found[: query.limit]

    async def get_pull_request(self, repo: RepoRef, number: int) -> PullRequest:
        try:
            return self.pulls[(repo.key, number)]
        except KeyError:
            raise CodeHostNotFoundError(f"{repo.full_name}!{number}") from None

    async def merge_pull_request(
        self, repo: RepoRef, number: int, method: MergeMethod
    ) -> MergeResult:
        pull = await self.get_pull_request(repo, number)
        if pull.state != PullRequestState.OPEN or pull.draft or pull.mergeable is False:
            raise CodeHostConflictError(f"{repo.full_name}!{number} can't be merged")
        self.pulls[(repo.key, number)] = pull.model_copy(update={"state": PullRequestState.MERGED})
        if pull.base_branch == self.default_branch:  # like GitHub: linked issues close themselves
            for issue_number in pull.closes_issues:
                await self.update_issue(
                    repo,
                    issue_number,
                    IssuePatch(state=IssueState.CLOSED, close_reason=CloseReason.COMPLETED),
                )
        return MergeResult(merged=True, sha="f" * 40, message=f"merged with {method}")

    async def aclose(self) -> None:
        pass
