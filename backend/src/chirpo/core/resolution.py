"""From what a message said to concrete GitHub terms, or one question back (D47).

The model names things as the message did ("Franck", "the checkout bug", "urgent"). Here they are
matched against this workspace only: its members (by verified GitHub login), its linked repos,
the repo's open issues and existing labels. Anything unclear becomes a question, never a guess.
"""

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date

from chirpo.core import messages
from chirpo.core.actions import (
    AddLabels,
    AskClarifyingQuestion,
    AssignIssue,
    CreateIssue,
    CreateIssues,
    IssueDraft,
    IssueRef,
    ListIssues,
    MyIssues,
    NotARequest,
    OnIssue,
    Proposal,
    RemoveLabels,
    ReopenIssue,
    RepoScoped,
    SetStatus,
    SmallTalk,
    UnassignIssue,
)
from chirpo.core.context import MAX_NAME, Team, clean
from chirpo.core.domain import Issue, IssueQuery, IssueState, Milestone, RepoRef
from chirpo.core.errors import CodeHostNotFoundError
from chirpo.core.outbound import shown
from chirpo.core.ports import CodeHostAdapter
from chirpo.core.statuses import StatusLabels, status_named
from chirpo.core.workspaces import Member

ME = frozenset({"me", "myself", "i"})
# Ways of saying "the sprint we're in" (M6.3).
CURRENT_MILESTONE = frozenset(
    {
        "current",
        "sprint",
        "this sprint",
        "current sprint",
        "the current sprint",
        "the sprint",
        "this milestone",
        "current milestone",
        "the current milestone",
    }
)
MAX_MILESTONES_NAMED = 5
_IGNORED_WORDS = frozenset(
    {"the", "a", "an", "issue", "bug", "ticket", "one", "that", "this", "about", "with", "for",
     "on", "in", "of", "to", "and", "is", "it"}
)  # fmt: skip
MAX_OPTIONS = 3
MAX_QUESTION = 500
MAX_TITLE = 80
MATCH_SEARCH = 100  # issues searched when one is named by description (GitHub's page size)


@dataclass(frozen=True, slots=True)
class Resolved:
    """A proposal in GitHub terms: people as logins, the issue as its number, existing labels."""

    proposal: Proposal
    repo: RepoRef
    issue: Issue | None = None  # the issue acted on, to show its title
    login: str | None = None  # the sender's, for "my issues"
    notes: tuple[str, ...] = ()  # what was left out, e.g. labels the repo doesn't have
    ref: IssueRef | None = None  # how the model named the issue, before it became a number
    milestone: Milestone | None = None  # the sprint a list is limited to (M6.3)


@dataclass(frozen=True, slots=True)
class Question:
    text: str


class _Unclear(Exception):
    def __init__(self, question: str) -> None:
        super().__init__(question)
        self.question = question


CanRead = Callable[[RepoRef], Awaitable[bool]]


async def resolve(
    proposals: Sequence[Proposal],
    *,
    team: Team,
    sender: Member,
    codehost: CodeHostAdapter,
    can_read: CanRead,
) -> list[Resolved] | Question | None:
    """The proposals resolved in order; the model's own question, or the first problem, instead.

    `can_read` is asked before anything is read from, or said about, a repo other than those
    already allowed; when it says no it has told the person why, and None comes back.
    """
    for proposal in proposals:
        if isinstance(proposal, AskClarifyingQuestion):
            return Question(shown(proposal.question, MAX_QUESTION))
    resolver = _Resolver(team, sender, codehost, can_read)
    try:
        return [
            await resolver.one(proposal)
            for proposal in proposals
            if not isinstance(proposal, NotARequest | SmallTalk)
        ]
    except _Unclear as unclear:
        return Question(unclear.question)
    except _NotAllowed:
        return None


@dataclass
class _Resolver:
    team: Team
    sender: Member
    codehost: CodeHostAdapter
    can_read: CanRead
    _readable: set[str] = field(default_factory=set)
    _labels: dict[str, list[str]] = field(default_factory=dict)

    async def one(self, proposal: Proposal) -> Resolved:
        if not isinstance(proposal, RepoScoped):  # only questions aren't, handled above
            raise TypeError(f"can't resolve {type(proposal).__name__}")
        repo = await self._repo(proposal.repo)
        update: dict[str, object] = {"repo": repo.full_name}
        notes: list[str] = []
        issue, ref, milestone = None, None, None
        if isinstance(proposal, OnIssue):
            ref = proposal.issue
            issue = await self._issue(
                repo, proposal.issue, closed=isinstance(proposal, ReopenIssue)
            )
            update["issue"] = IssueRef(number=issue.number)
        match proposal:
            case CreateIssue():
                update.update(await self._draft(repo, proposal, notes))
            case CreateIssues():
                drafts = [
                    _revalidated(draft, await self._draft(repo, draft, notes))
                    for draft in proposal.issues
                ]
                update["issues"] = tuple(drafts)
            case AssignIssue() | UnassignIssue():
                update["assignees"] = self._logins(proposal.assignees)
            case SetStatus():
                update["status"] = _status_label(proposal.status, self.team.statuses)
            case AddLabels() | RemoveLabels():
                update["labels"] = await self._labels_or_ask(repo, proposal.labels)
            case ListIssues():
                if proposal.assignee:
                    update["assignee"] = self._login(proposal.assignee)
                if proposal.labels:
                    update["labels"] = await self._labels_or_ask(repo, proposal.labels)
        if isinstance(proposal, ListIssues | MyIssues) and proposal.milestone:
            milestone = await self._milestone(repo, proposal.milestone)
        login = self._login("me") if isinstance(proposal, MyIssues) else None
        return Resolved(
            proposal=_revalidated(proposal, update),
            repo=repo,
            issue=issue,
            login=login,
            notes=tuple(notes),
            ref=ref,
            milestone=milestone,
        )

    async def _milestone(self, repo: RepoRef, wanted: str) -> Milestone:
        """A title first, so a milestone named "Sprint" can be picked; then "current" (or "this
        sprint"): the open milestone due soonest, overdue ones included, since an open sprint
        isn't over (D59). Only open milestones are searched."""
        found = await self.codehost.list_milestones(repo)
        if not found:
            raise _Unclear(messages.NO_MILESTONE.format(repo=repo.full_name))
        name = _normal(wanted)
        same = [m for m in found if _normal(m.title) == name]
        if len(same) == 1:
            return same[0]
        if same:
            raise _Unclear(
                _short(
                    messages.MILESTONE_DUPLICATE.format(
                        count=len(same), name=shown(wanted, MAX_NAME), repo=repo.full_name
                    )
                )
            )
        names = ", ".join(shown(m.title, MAX_NAME) for m in found[:MAX_MILESTONES_NAMED])
        if name in CURRENT_MILESTONE:
            dated = [m for m in found if m.due_on is not None]
            if dated:
                return min(dated, key=lambda m: m.due_on or date.max)
            if len(found) == 1:
                return found[0]
            raise _Unclear(_short(messages.WHICH_MILESTONE.format(names=names)))
        raise _Unclear(
            _short(
                messages.MILESTONE_UNKNOWN.format(
                    name=shown(wanted, MAX_NAME), repo=repo.full_name, names=names
                )
            )
        )

    async def _repo(self, name: str | None) -> RepoRef:
        links = list(self.team.repos)
        if name is None:
            repo = self.team.default_repo
        else:
            wanted = name.strip().casefold()
            found = [
                link.repo
                for link in links
                if wanted in (link.repo.full_name.casefold(), link.repo.name.casefold())
            ]
            if not found:
                repos = ", ".join(link.repo.full_name for link in links)
                raise _Unclear(messages.REPO_NOT_LINKED.format(name=name, repos=repos))
            if len(found) > 1:
                options = ", ".join(repo.full_name for repo in found)
                raise _Unclear(messages.REPO_AMBIGUOUS.format(name=name, options=options))
            repo = found[0]
        if repo.key not in self._readable:
            if not await self.can_read(repo):
                raise _NotAllowed
            self._readable.add(repo.key)
        return repo

    async def _issue(self, repo: RepoRef, ref: IssueRef, *, closed: bool) -> Issue:
        if ref.number is not None:
            try:
                return await self.codehost.get_issue(repo, ref.number)
            except CodeHostNotFoundError:
                raise _Unclear(
                    messages.ISSUE_NOT_FOUND_NUMBER.format(number=ref.number, repo=repo.full_name)
                ) from None
        description = ref.description or ""
        state = IssueState.CLOSED if closed else IssueState.OPEN
        candidates = await self.codehost.list_issues(
            repo, IssueQuery(state=state, limit=MATCH_SEARCH)
        )
        wanted = _words(description)
        needed = max(1, (len(wanted) + 1) // 2)  # at least half the words that say something
        scores = [(len(wanted & _words(issue.title)), issue) for issue in candidates]
        best = max((score for score, _ in scores), default=0)
        matches = [issue for score, issue in scores if score == best and best >= needed]
        if not matches:
            text = messages.ISSUE_NOT_FOUND_CLOSED if closed else messages.ISSUE_NOT_FOUND
            raise _Unclear(text.format(description=description))
        if len(matches) > 1:
            options = ", ".join(
                f'#{i.number} "{shown(i.title, MAX_TITLE)}"' for i in matches[:MAX_OPTIONS]
            )
            raise _Unclear(
                messages.ISSUE_AMBIGUOUS.format(description=description, options=options)
            )
        return matches[0]

    async def _draft(self, repo: RepoRef, draft: IssueDraft, notes: list[str]) -> dict[str, object]:
        labels, dropped = await self._existing_labels(repo, draft.labels)
        notes += self._left_out(repo, dropped)
        return {"assignees": self._logins(draft.assignees), "labels": labels}

    async def _labels_or_ask(self, repo: RepoRef, wanted: Sequence[str]) -> tuple[str, ...]:
        """Only existing labels; asks when none of the wanted ones exist (never silently none)."""
        labels, dropped = await self._existing_labels(repo, wanted)
        if dropped:
            raise _Unclear(
                messages.NO_SUCH_LABELS.format(
                    labels=_quoted(dropped),
                    repo=repo.full_name,
                    existing=", ".join(shown(n, MAX_NAME) for n in await self._repo_labels(repo)),
                )
            )
        return labels

    def _logins(self, names: Sequence[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self._login(name) for name in names))

    def _login(self, name: str) -> str:
        member = self._member(name)
        if member.codehost is None:
            name = shown(member.display_name, MAX_NAME)
            raise _Unclear(messages.PERSON_NOT_LINKED.format(name=name))
        return member.codehost.login

    def _member(self, name: str) -> Member:
        wanted = " ".join(name.strip().removeprefix("@").casefold().split())
        if wanted in ME:
            return self.sender
        members = list(self.team.members)
        found = [m for m in members if wanted in _handles(m)] or [
            m for m in members if wanted in _name_forms(m.display_name)
        ]
        if not found:
            raise _Unclear(messages.PERSON_UNKNOWN.format(name=name))
        if len(found) > 1:
            options = ", ".join(_person(m) for m in found[:MAX_OPTIONS])
            raise _Unclear(messages.PERSON_AMBIGUOUS.format(name=name, options=options))
        return found[0]

    async def _existing_labels(
        self, repo: RepoRef, wanted: Sequence[str]
    ) -> tuple[tuple[str, ...], list[str]]:
        if not wanted:
            return (), []
        by_fold = {name.casefold(): name for name in await self._repo_labels(repo)}
        kept = tuple(
            dict.fromkeys(by_fold[w.casefold()] for w in wanted if w.casefold() in by_fold)
        )
        dropped = [w for w in wanted if w.casefold() not in by_fold]
        return kept, dropped

    async def _repo_labels(self, repo: RepoRef) -> list[str]:
        if repo.key not in self._labels:
            names = await self.codehost.list_labels(repo)
            # Labels with commas can't be used in filters (they're comma-joined) or proposals.
            usable = [clean(name, MAX_NAME) for name in names if "," not in name]
            self._labels[repo.key] = sorted(usable, key=str.casefold)
        return self._labels[repo.key]

    @staticmethod
    def _left_out(repo: RepoRef, dropped: Sequence[str]) -> list[str]:
        if not dropped:
            return []
        return [messages.LABELS_LEFT_OUT.format(labels=_quoted(dropped), repo=repo.full_name)]


class _NotAllowed(Exception):
    """The sender may not read a repo the request names; they were already told why."""


def _revalidated[P: Proposal](proposal: P, update: dict[str, object]) -> P:
    """Resolved values go through the models' validation again, as anything to be run must."""
    return type(proposal).model_validate({**proposal.model_dump(), **update})


def _person(member: Member) -> str:
    name = shown(member.display_name, MAX_NAME)
    return f"{name} (GitHub {member.codehost.login})" if member.codehost else name


def _name_forms(display_name: str) -> set[str]:
    words = display_name.casefold().split()
    return {" ".join(words), *words}


def _handles(member: Member) -> set[str]:
    handles = {member.chat_username, member.codehost.login if member.codehost else None}
    return {handle.casefold() for handle in handles if handle}


def _status_label(said: str, labels: StatusLabels) -> str:
    status = status_named(said, labels)
    if status is None:
        raise _Unclear(messages.STATUS_UNKNOWN.format(status=shown(said, MAX_NAME)))
    return labels.label(status)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[\w']+", text.casefold()) if w not in _IGNORED_WORDS}


def significant_words(text: str) -> set[str]:
    """The words of `text` that say something (no "the", "issue", "bug"...), lower-cased."""
    return _words(text)


def _quoted(names: Sequence[str]) -> str:
    return ", ".join(f'"{shown(name, MAX_NAME)}"' for name in names)


def _normal(text: str) -> str:
    return " ".join(text.casefold().split())


def _short(question: str) -> str:
    return shown(question, MAX_QUESTION)
