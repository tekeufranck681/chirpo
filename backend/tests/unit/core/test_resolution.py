"""From what the message said to concrete GitHub terms, or one question back (D47)."""

from typing import Any
from uuid import uuid4

import pytest

from chirpo.core import messages
from chirpo.core.actions import (
    AddLabels,
    AskClarifyingQuestion,
    AssignIssue,
    CloseIssue,
    CreateIssue,
    CreateIssues,
    IssueRef,
    ListIssues,
    MyIssues,
    NotARequest,
    Proposal,
    ReopenIssue,
    SetStatus,
)
from chirpo.core.context import Team
from chirpo.core.domain import Issue, IssuePatch, IssueState, NewIssue, RepoRef
from chirpo.core.permissions import Role
from chirpo.core.resolution import Question, Resolved, resolve
from chirpo.core.workspaces import CodeHostAccount, LinkedRepo, Member
from tests.fakes import FakeCodeHost

SANDBOX = RepoRef.parse("github", "tekeufranck681/chirpo-sandbox")
OTHER = RepoRef.parse("github", "tekeufranck681/chirpo")
WORKSPACE = uuid4()


def person(name: str, username: str | None, login: str | None, user_id: str = "1") -> Member:
    account = CodeHostAccount(provider="github", user_id=user_id, login=login) if login else None
    return Member(
        id=uuid4(),
        workspace_id=WORKSPACE,
        display_name=name,
        role=Role.MEMBER,
        chat_username=username,
        codehost=account,
    )


FRANCK = person("Tekeu Franck", "franck_tg", "tekeufranck681", "186301755")
ANNIE = person("Annie Ngo", "annie_n", "annie-gh", "2")
NEWCOMER = person("Boris", "boris_b", None)


class World:
    def __init__(self) -> None:
        self.codehost = FakeCodeHost()
        self.codehost.labels[SANDBOX.key] = {"bug", "Enhancement", "status: done"}
        self.codehost.labels[OTHER.key] = {"bug"}
        self.members = [FRANCK, ANNIE, NEWCOMER]
        self.issues: list[Issue] = []
        self.unreadable: set[str] = set()
        self.read_checks: list[RepoRef] = []

    async def issue(self, title: str, repo: RepoRef = SANDBOX) -> Issue:
        created = await self.codehost.create_issue(repo, NewIssue(title=title))
        if repo == SANDBOX:
            self.issues.insert(0, created)
        return created

    async def resolve(
        self, *proposals: Proposal, sender: Member = FRANCK
    ) -> list[Resolved] | Question | None:
        team = Team(
            members=self.members,
            repos=[
                LinkedRepo(repo=SANDBOX, is_default=True),
                LinkedRepo(repo=OTHER, is_default=False),
            ],
            open_issues=self.issues,
        )

        async def can_read(repo: RepoRef) -> bool:
            self.read_checks.append(repo)
            return repo.key not in self.unreadable

        return await resolve(
            proposals, team=team, sender=sender, codehost=self.codehost, can_read=can_read
        )

    async def one(self, proposal: Proposal, sender: Member = FRANCK) -> Resolved:
        result = await self.resolve(proposal, sender=sender)
        assert isinstance(result, list), result
        [resolved] = result
        return resolved

    async def question(self, proposal: Proposal, sender: Member = FRANCK) -> str:
        result = await self.resolve(proposal, sender=sender)
        assert isinstance(result, Question), result
        return result.text


@pytest.fixture
def world() -> World:
    return World()


def create(**fields: Any) -> CreateIssue:
    return CreateIssue.model_validate({"title": "Checkout crashes", **fields})


# ------------------------------------------------------------------ people


@pytest.mark.parametrize(
    "name", ["me", "Me", "Franck", "franck", "Tekeu Franck", "@franck_tg", "tekeufranck681"]
)
async def test_people_by_any_name(world: World, name: str) -> None:
    resolved = await world.one(create(assignees=[name]))
    assert isinstance(resolved.proposal, CreateIssue)
    assert resolved.proposal.assignees == ("tekeufranck681",)


async def test_me_is_the_sender(world: World) -> None:
    resolved = await world.one(create(assignees=["me"]), sender=ANNIE)
    assert resolved.proposal.assignees == ("annie-gh",)  # type: ignore[attr-defined]


async def test_an_unknown_person_is_asked_about(world: World) -> None:
    assert await world.question(create(assignees=["Kofi"])) == messages.PERSON_UNKNOWN.format(
        name="Kofi"
    )


async def test_someone_without_github_is_asked_about(world: World) -> None:
    assert await world.question(create(assignees=["Boris"])) == (
        messages.PERSON_NOT_LINKED.format(name="Boris")
    )


async def test_a_name_shared_by_two_people_is_asked_about(world: World) -> None:
    world.members.append(person("Franck Mbida", "fm", "franck-m", "3"))
    assert await world.question(create(assignees=["Franck"])) == (
        messages.PERSON_AMBIGUOUS.format(
            name="Franck",
            options="Tekeu Franck (GitHub tekeufranck681), Franck Mbida (GitHub franck-m)",
        )
    )


async def test_an_exact_handle_wins_over_a_shared_first_name(world: World) -> None:
    world.members.append(person("Franck Mbida", "fm", "franck-m", "3"))
    resolved = await world.one(create(assignees=["@franck_tg"]))
    assert resolved.proposal.assignees == ("tekeufranck681",)  # type: ignore[attr-defined]


async def test_an_unlinked_sender_saying_me(world: World) -> None:
    assert await world.question(create(assignees=["me"]), sender=NEWCOMER) == (
        messages.PERSON_NOT_LINKED.format(name="Boris")
    )


# ------------------------------------------------------------------ repos


async def test_the_default_repo(world: World) -> None:
    resolved = await world.one(create())
    assert resolved.repo == SANDBOX


@pytest.mark.parametrize("name", ["chirpo", "tekeufranck681/chirpo", "CHIRPO"])
async def test_a_named_repo(world: World, name: str) -> None:
    resolved = await world.one(create(repo=name))
    assert resolved.repo == OTHER
    assert resolved.proposal.repo == OTHER.full_name  # type: ignore[attr-defined]


async def test_a_repo_not_linked_here(world: World) -> None:
    assert await world.question(create(repo="secret-repo")) == messages.REPO_NOT_LINKED.format(
        name="secret-repo", repos="tekeufranck681/chirpo-sandbox, tekeufranck681/chirpo"
    )


# ------------------------------------------------------------------ issues


async def test_an_issue_by_number(world: World) -> None:
    issue = await world.issue("Checkout crashes on Android 13")
    resolved = await world.one(AssignIssue(issue=IssueRef(number=issue.number), assignees=("me",)))
    assert resolved.issue == issue


async def test_an_issue_that_does_not_exist(world: World) -> None:
    assert await world.question(CloseIssue(issue=IssueRef(number=99))) == (
        messages.ISSUE_NOT_FOUND_NUMBER.format(number=99, repo=SANDBOX.full_name)
    )


async def test_an_issue_by_description(world: World) -> None:
    await world.issue("Add dark mode")
    checkout = await world.issue("Checkout crashes on Android 13")
    resolved = await world.one(CloseIssue(issue=IssueRef(description="the checkout bug")))
    assert resolved.issue == checkout
    assert resolved.proposal.issue == IssueRef(number=checkout.number)  # type: ignore[attr-defined]


async def test_a_description_matching_several_issues(world: World) -> None:
    first = await world.issue("Checkout crashes on Android")
    second = await world.issue("Checkout button is grey")
    text = await world.question(CloseIssue(issue=IssueRef(description="checkout")))
    assert text == messages.ISSUE_AMBIGUOUS.format(
        description="checkout",
        options=f'#{first.number} "Checkout crashes on Android", '
        f'#{second.number} "Checkout button is grey"',
    )


async def test_a_description_matching_nothing(world: World) -> None:
    await world.issue("Add dark mode")
    assert await world.question(CloseIssue(issue=IssueRef(description="the login bug"))) == (
        messages.ISSUE_NOT_FOUND.format(description="the login bug")
    )


async def test_a_description_in_another_repo_reads_its_issues(world: World) -> None:
    other = await world.issue("Login fails with SSO", repo=OTHER)
    resolved = await world.one(
        CloseIssue(issue=IssueRef(description="the SSO login"), repo="chirpo")
    )
    assert resolved.issue == other


# ------------------------------------------------------------------ labels and status


async def test_only_existing_labels_are_kept(world: World) -> None:
    resolved = await world.one(create(labels=["BUG", "urgent", "enhancement"]))
    assert resolved.proposal.labels == ("bug", "Enhancement")  # type: ignore[attr-defined]
    assert resolved.notes == (
        messages.LABELS_LEFT_OUT.format(labels='"urgent"', repo=SANDBOX.full_name),
    )


async def test_adding_only_unknown_labels_is_asked_about(world: World) -> None:
    issue = await world.issue("Add dark mode")
    proposal = AddLabels(issue=IssueRef(number=issue.number), labels=("urgent",))
    assert await world.question(proposal) == messages.NO_SUCH_LABELS.format(
        labels='"urgent"', repo=SANDBOX.full_name, existing="bug, Enhancement, status: done"
    )


@pytest.mark.parametrize(
    ("said", "label"),
    [
        ("done", "status: done"),
        ("Finished", "status: done"),
        ("in-progress", "status: in progress"),
        ("WIP", "status: in progress"),
        ("to do", "status: todo"),
        ("needs review", "status: review"),
        ("status: review", "status: review"),
    ],
)
async def test_statuses(world: World, said: str, label: str) -> None:
    issue = await world.issue("Add dark mode")
    resolved = await world.one(SetStatus(issue=IssueRef(number=issue.number), status=said))
    assert resolved.proposal.status == label  # type: ignore[attr-defined]


async def test_an_unknown_status(world: World) -> None:
    issue = await world.issue("Add dark mode")
    proposal = SetStatus(issue=IssueRef(number=issue.number), status="parked")
    assert await world.question(proposal) == messages.STATUS_UNKNOWN.format(status="parked")


# ------------------------------------------------------------------ the rest


async def test_several_issues_at_once(world: World) -> None:
    proposal = CreateIssues.model_validate(
        {"issues": [{"title": "a", "assignees": ["Annie"]}, {"title": "b", "labels": ["bug"]}]}
    )
    resolved = await world.one(proposal)
    first, second = resolved.proposal.issues  # type: ignore[attr-defined]
    assert first.assignees == ("annie-gh",)
    assert second.labels == ("bug",)


async def test_listing_someone_elses_issues(world: World) -> None:
    resolved = await world.one(ListIssues(assignee="Annie"))
    assert resolved.proposal.assignee == "annie-gh"  # type: ignore[attr-defined]


async def test_my_issues_are_the_senders(world: World) -> None:
    resolved = await world.one(MyIssues())
    assert resolved.login == "tekeufranck681"


async def test_a_question_from_the_model_is_passed_on(world: World) -> None:
    result = await world.resolve(create(), AskClarifyingQuestion(question="Which repo?"))
    assert result == Question("Which repo?")


async def test_not_a_request_alone_resolves_to_nothing(world: World) -> None:
    assert await world.resolve(NotARequest()) == []


async def test_not_a_request_next_to_real_actions_is_dropped(world: World) -> None:
    result = await world.resolve(NotARequest(), create())
    assert isinstance(result, list)
    assert len(result) == 1


async def test_the_first_problem_is_the_question(world: World) -> None:
    result = await world.resolve(create(assignees=["Kofi"]), create(repo="nowhere"))
    assert result == Question(messages.PERSON_UNKNOWN.format(name="Kofi"))


# ------------------------------------------------------------------ review fixes (D47)


async def test_a_repo_the_sender_cannot_read_is_never_touched(world: World) -> None:
    world.unreadable.add(OTHER.key)
    proposal = AddLabels(issue=IssueRef(description="login"), labels=("nope",), repo="chirpo")
    assert await world.resolve(proposal) is None
    assert world.read_checks == [OTHER]


async def test_each_repo_is_checked_once(world: World) -> None:
    await world.resolve(create(), create(title="b"), create(repo="chirpo"))
    assert world.read_checks == [SANDBOX, OTHER]


async def test_reopening_by_description_looks_at_closed_issues(world: World) -> None:
    issue = await world.issue("Checkout crashes on Android 13")
    await world.codehost.update_issue(SANDBOX, issue.number, IssuePatch(state=IssueState.CLOSED))
    resolved = await world.one(ReopenIssue(issue=IssueRef(description="the checkout crash")))
    assert resolved.issue is not None
    assert resolved.issue.number == issue.number


async def test_one_shared_word_out_of_several_is_not_a_match(world: World) -> None:
    await world.issue("Android build fails")
    text = await world.question(CloseIssue(issue=IssueRef(description="checkout crash on android")))
    assert text == messages.ISSUE_NOT_FOUND.format(description="checkout crash on android")


async def test_issues_beyond_the_context_are_still_found(world: World) -> None:
    for n in range(40):
        await world.issue(f"Filler {n}")
    world.issues = world.issues[:30]
    old = await world.codehost.get_issue(SANDBOX, 1)
    resolved = await world.one(CloseIssue(issue=IssueRef(description="filler 0")))
    assert resolved.issue == old


async def test_a_list_filtered_by_unknown_labels_asks(world: World) -> None:
    assert await world.question(ListIssues(labels=("urgent",))) == messages.NO_SUCH_LABELS.format(
        labels='"urgent"', repo=SANDBOX.full_name, existing="bug, Enhancement, status: done"
    )


async def test_two_linked_repos_with_the_same_name(world: World) -> None:
    fork = RepoRef.parse("github", "annie/chirpo")
    team = Team(
        members=world.members,
        repos=[
            LinkedRepo(repo=SANDBOX, is_default=True),
            LinkedRepo(repo=OTHER, is_default=False),
            LinkedRepo(repo=fork, is_default=False),
        ],
        open_issues=[],
    )

    async def yes(repo: RepoRef) -> bool:
        return True

    result = await resolve(
        [create(repo="chirpo")], team=team, sender=FRANCK, codehost=world.codehost, can_read=yes
    )
    assert result == Question(
        messages.REPO_AMBIGUOUS.format(name="chirpo", options="tekeufranck681/chirpo, annie/chirpo")
    )


async def test_repo_labels_with_commas_are_never_used(world: World) -> None:
    world.codehost.labels[SANDBOX.key].add("a,b")
    resolved = await world.one(create(labels=["bug"]))
    assert resolved.proposal.labels == ("bug",)  # type: ignore[attr-defined]


async def test_someone_without_github_or_username_is_listed_by_name(world: World) -> None:
    world.members += [person("Annie Ngo", None, None, "8")]
    text = await world.question(create(assignees=["Annie"]))
    assert "None" not in text
    assert "Annie Ngo (GitHub annie-gh), Annie Ngo" in text


async def test_resolved_proposals_are_validated_again(world: World) -> None:
    resolved = await world.one(create(assignees=["me"], labels=["bug"]))
    assert type(resolved.proposal).model_validate(resolved.proposal.model_dump()) == (
        resolved.proposal
    )
