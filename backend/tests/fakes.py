"""Fake adapters for testing the core without network calls."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

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
from chirpo.core.errors import (
    CodeHostConflictError,
    CodeHostForbiddenError,
    CodeHostNotFoundError,
    LastAdminError,
    MessagingForbiddenError,
)
from chirpo.core.permissions import Role, role_change_refusal
from chirpo.core.workspaces import AuditEntry, CodeHostAccount, LinkedRepo, Member, Workspace

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
        self.unreachable_chats: set[str] = set()  # e.g. private chats never opened with the bot
        self.chat_admins: set[str] = set()  # user ids that run the group

    def parse_update(self, raw: Mapping[str, Any]) -> InboundEvent | None:
        return None

    async def send_message(
        self, chat: ChatRef, text: str, *, reply_to: str | None = None
    ) -> SentMessage:
        if chat.chat_id in self.unreachable_chats:
            raise MessagingForbiddenError(f"can't message {chat.chat_id}")
        self.sent.append((chat, text, reply_to))
        return SentMessage(chat=chat, message_id=str(100 + len(self.sent)))

    async def edit_message(self, message: SentMessage, text: str) -> None:
        self.edited.append((message, text))

    def private_chat(self, user: ChatUser) -> ChatRef:
        return ChatRef(platform=self.platform, chat_id=user.user_id)

    def private_chat_link(self) -> str:
        return "https://chat.example/chirpo?start=iam"

    async def is_chat_admin(self, chat: ChatRef, user: ChatUser) -> bool:
        return user.user_id in self.chat_admins

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
        self.logins: dict[str, str] = {}  # account id -> current login
        self.not_installed: set[str] = set()  # repo keys the App can't reach
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

    async def current_login(self, repo: RepoRef, user_id: str) -> str | None:
        if repo.key in self.not_installed:
            raise CodeHostForbiddenError(f"The GitHub App is not installed on {repo.full_name}")
        return self.logins.get(user_id)

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


class FakeWorkspaces:
    """In-memory Workspaces with the SQL semantics (case-insensitive repos, one default)."""

    def __init__(self, members: "FakeMembers | None" = None) -> None:
        self.by_chat: dict[tuple[str, str], Workspace] = {}
        self.links: dict[UUID, list[LinkedRepo]] = {}
        self.members = members  # to promote the first linker, like the SQL transaction does

    async def for_chat(self, chat: ChatRef) -> Workspace | None:
        return self.by_chat.get((chat.platform, chat.chat_id))

    async def get_or_create_for_chat(self, chat: ChatRef, *, name: str) -> Workspace:
        return self.by_chat.setdefault(
            (chat.platform, chat.chat_id), Workspace(id=uuid4(), name=name)
        )

    async def repos(self, workspace_id: UUID) -> list[LinkedRepo]:
        links = self.links.get(workspace_id, [])
        return sorted(links, key=lambda link: not link.is_default)

    async def link_repo(
        self,
        workspace_id: UUID,
        repo: RepoRef,
        *,
        installation_id: int | None = None,
        first_admin: UUID | None = None,
    ) -> LinkedRepo | None:
        links = self.links.setdefault(workspace_id, [])
        if first_admin is not None:
            if links:
                return None
            if self.members is not None:
                await self.members.set_role(workspace_id, first_admin, Role.ADMIN)
        for index, link in enumerate(links):
            if link.repo.key == repo.key:
                if installation_id is not None:
                    links[index] = link.model_copy(update={"installation_id": installation_id})
                return links[index]
        link = LinkedRepo(repo=repo, is_default=not links, installation_id=installation_id)
        links.append(link)
        return link

    async def unlink_repo(self, workspace_id: UUID, repo: RepoRef) -> bool:
        links = self.links.get(workspace_id, [])
        for index, link in enumerate(links):
            if link.repo.key == repo.key:
                del links[index]
                if link.is_default and links:
                    links[0] = links[0].model_copy(update={"is_default": True})
                return True
        return False


class FakeMembers:
    def __init__(self) -> None:
        self.members: dict[UUID, Member] = {}
        self.chat_accounts: dict[tuple[UUID, str, str], UUID] = {}

    async def for_chat_user(self, workspace_id: UUID, user: ChatUser) -> Member:
        key = (workspace_id, user.platform, user.user_id)
        if key in self.chat_accounts:
            member = self.members[self.chat_accounts[key]].model_copy(
                update={"display_name": user.display_name, "chat_username": user.username}
            )
        else:
            member = Member(
                id=uuid4(),
                workspace_id=workspace_id,
                display_name=user.display_name,
                role=Role.MEMBER,
                chat_username=user.username,
            )
            self.chat_accounts[key] = member.id
        self.members[member.id] = member
        return member

    async def all(self, workspace_id: UUID) -> list[Member]:
        return [member for member in self.members.values() if member.workspace_id == workspace_id]

    async def set_role(self, workspace_id: UUID, member_id: UUID, role: Role) -> Member | None:
        target = self._get(workspace_id, member_id)
        if target is None:
            return None
        admins = sum(m.role == Role.ADMIN for m in await self.all(workspace_id))
        if role_change_refusal(target, role, admins):
            raise LastAdminError
        return self._update(workspace_id, member_id, role=role)

    async def link_codehost(
        self, workspace_id: UUID, member_id: UUID, account: CodeHostAccount
    ) -> Member | None:
        if self._get(workspace_id, member_id) is None:
            return None
        same = (account.provider, account.user_id)
        for other in await self.all(workspace_id):
            if other.codehost and (other.codehost.provider, other.codehost.user_id) == same:
                self._update(workspace_id, other.id, codehost=None)
        return self._update(workspace_id, member_id, codehost=account)

    def _get(self, workspace_id: UUID, member_id: UUID) -> Member | None:
        member = self.members.get(member_id)
        return member if member and member.workspace_id == workspace_id else None

    def _update(self, workspace_id: UUID, member_id: UUID, **changes: Any) -> Member | None:
        member = self._get(workspace_id, member_id)
        if member is None:
            return None
        self.members[member_id] = member.model_copy(update=changes)
        return self.members[member_id]


class FakeAuditLog:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)
