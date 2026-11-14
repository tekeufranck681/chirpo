"""Running confirmed steps on the code host: what each action does, what is audited, and when a
run stops (M5.5). The stored steps are run for the person who asked, with their permissions."""

from dataclasses import replace

import pytest

from chirpo.core import messages
from chirpo.core.actions import (
    AddLabels,
    AssignIssue,
    CloseIssue,
    CommentOnIssue,
    CreateIssue,
    CreateIssues,
    IssueDraft,
    IssueRef,
    Proposal,
    RemoveLabels,
    ReopenIssue,
    SetStatus,
    UnassignIssue,
)
from chirpo.core.commands import Services
from chirpo.core.domain import (
    AuditResult,
    ChatUser,
    CloseReason,
    IssueState,
    NewIssue,
    RepoPermission,
    RepoRef,
)
from chirpo.core.executor import Outcome, execute, undo
from chirpo.core.pending import Step
from chirpo.core.permissions import PermissionChecker, Role
from chirpo.core.ratelimit import Limits, RateLimiter
from chirpo.core.statuses import StatusLabels
from chirpo.core.workspaces import CodeHostAccount, Member
from tests.fakes import (
    CHAT,
    FakeAuditLog,
    FakeCodeHost,
    FakeCounters,
    FakeMembers,
    FakePendingActions,
    FakeWorkspaces,
)

REPO = RepoRef.parse("github", "tekeufranck681/chirpo-sandbox")
ZWSP = "​"
FRANCK = ChatUser(platform="fake", user_id="42", display_name="Franck", username="franck_tg")
ANNIE = ChatUser(platform="fake", user_id="77", display_name="Annie", username="annie_n")


class Setup:
    def __init__(self) -> None:
        self.members = FakeMembers()
        self.workspaces = FakeWorkspaces(self.members)
        self.audit = FakeAuditLog()
        self.codehost = FakeCodeHost()
        self.codehost.provider = "github"
        self.codehost.users = {"franck-gh", "annie-gh"}
        self.codehost.logins = {"1": "franck-gh", "2": "annie-gh"}
        self.codehost.permissions[(REPO.key, "franck-gh")] = RepoPermission.WRITE
        self.services = Services(
            workspaces=self.workspaces,
            members=self.members,
            audit=self.audit,
            codehost=self.codehost,
            permissions=PermissionChecker(self.codehost),
            pending=FakePendingActions(),
        )
        self.franck: Member
        self.annie: Member

    async def ready(self) -> "Setup":
        workspace = await self.workspaces.get_or_create_for_chat(CHAT, name="Shop")
        await self.workspaces.link_repo(workspace.id, REPO)
        self.franck = await self.members.for_chat_user(workspace.id, FRANCK)
        self.franck = (
            await self.members.link_codehost(
                workspace.id,
                self.franck.id,
                CodeHostAccount(provider="github", user_id="1", login="franck-gh"),
            )
            or self.franck
        )
        self.annie = await self.members.for_chat_user(workspace.id, ANNIE)
        self.annie = await self.members.set_role(workspace.id, self.annie.id, Role.MAINTAINER) or (
            self.annie
        )
        return self

    async def issue(self, **fields: object) -> int:
        created = await self.codehost.create_issue(
            REPO,
            NewIssue(title="Checkout crashes", **fields),  # type: ignore[arg-type]
        )
        return created.number

    async def outcome(self, *proposals: Proposal, confirmer: Member | None = None) -> Outcome:
        return await execute(
            [Step.of(p, REPO) for p in proposals],
            requester=self.franck,
            confirmer=confirmer,
            services=self.services,
            statuses=StatusLabels(),
        )

    async def run(self, *proposals: Proposal) -> "tuple[list[str], str | None]":
        outcome = await execute(
            [Step.of(p, REPO) for p in proposals],
            requester=self.franck,
            confirmer=self.annie,
            services=self.services,
            statuses=StatusLabels(),
        )
        return outcome.done, outcome.problem


async def setup() -> Setup:
    return await Setup().ready()


def on(number: int) -> IssueRef:
    return IssueRef(number=number)


REPO_NAME = REPO.full_name


async def test_create_issue_posts_clean_text_with_who_asked() -> None:
    s = await setup()
    done, problem = await s.run(
        CreateIssue(
            repo=REPO_NAME,
            title="Crash for @annie-gh",
            body="Ask @annie-gh. fixes #4. [docs](https://evil.example)",
            assignees=("annie-gh",),
            labels=("bug",),
        )
    )
    created = s.codehost.issues[(REPO.key, 1)]
    assert problem is None
    assert created.title == f"Crash for @{ZWSP}annie-gh"
    assert created.body == (
        f"Ask @{ZWSP}annie-gh. fixes{ZWSP} #4. \\[docs\\](https://evil.example)\n\n"
        + messages.REQUESTED_BY.format(login="franck-gh")
    )
    assert created.assignees == ("annie-gh",)
    assert created.labels == ("bug",)
    assert done == [messages.ISSUE_CREATED.format(number=1, title=created.title, url=created.url)]


async def test_an_issue_without_details_still_says_who_asked() -> None:
    s = await setup()
    await s.run(CreateIssue(repo=REPO_NAME, title="Fix it"))
    assert s.codehost.issues[(REPO.key, 1)].body == messages.REQUESTED_BY.format(login="franck-gh")


async def test_several_issues_are_all_created() -> None:
    s = await setup()
    drafts = (IssueDraft(title="One"), IssueDraft(title="Two", body="Details"))
    done, problem = await s.run(CreateIssues(repo=REPO_NAME, issues=drafts))
    assert problem is None
    assert [i.title for i in s.codehost.issues.values()] == ["One", "Two"]
    assert len(done) == 2


async def test_assign_adds_to_the_people_already_on_the_issue() -> None:
    s = await setup()
    number = await s.issue(assignees=("franck-gh",))
    await s.run(AssignIssue(repo=REPO_NAME, issue=on(number), assignees=("annie-gh",)))
    assert s.codehost.issues[(REPO.key, number)].assignees == ("franck-gh", "annie-gh")


async def test_assigning_someone_already_there_changes_nothing_twice() -> None:
    s = await setup()
    number = await s.issue(assignees=("annie-gh",))
    await s.run(AssignIssue(repo=REPO_NAME, issue=on(number), assignees=("Annie-GH",)))
    assert s.codehost.issues[(REPO.key, number)].assignees == ("annie-gh",)


async def test_unassign_removes_only_those_people() -> None:
    s = await setup()
    number = await s.issue(assignees=("franck-gh", "annie-gh"))
    await s.run(UnassignIssue(repo=REPO_NAME, issue=on(number), assignees=("ANNIE-GH",)))
    assert s.codehost.issues[(REPO.key, number)].assignees == ("franck-gh",)


async def test_status_replaces_the_old_status_and_keeps_other_labels() -> None:
    s = await setup()
    number = await s.issue(labels=("bug", "status: todo"))
    done, _ = await s.run(SetStatus(repo=REPO_NAME, issue=on(number), status="status: review"))
    assert set(s.codehost.issues[(REPO.key, number)].labels) == {"bug", "status: review"}
    assert "status: review" in s.codehost.labels[REPO.key]  # created for the repo when missing
    assert done == [
        messages.DONE_STATUS.format(issue=f'#{number} "Checkout crashes"', status="review")
    ]


async def test_labels_are_added_and_removed_without_touching_the_rest() -> None:
    s = await setup()
    number = await s.issue(labels=("bug",))
    await s.run(AddLabels(repo=REPO_NAME, issue=on(number), labels=("urgent", "BUG")))
    assert s.codehost.issues[(REPO.key, number)].labels == ("bug", "urgent")
    await s.run(RemoveLabels(repo=REPO_NAME, issue=on(number), labels=("BUG",)))
    assert s.codehost.issues[(REPO.key, number)].labels == ("urgent",)


async def test_a_comment_is_clean_and_says_who_asked() -> None:
    s = await setup()
    number = await s.issue()
    await s.run(CommentOnIssue(repo=REPO_NAME, issue=on(number), body="cc @annie-gh, fixes #2"))
    assert s.codehost.comments == [
        (
            REPO.key,
            number,
            f"cc @{ZWSP}annie-gh, fixes{ZWSP} #2\n\n"
            + messages.REQUESTED_BY.format(login="franck-gh"),
        )
    ]


@pytest.mark.parametrize("reason", list(CloseReason))
async def test_close_gives_the_reason_and_reopen_clears_it(reason: CloseReason) -> None:
    s = await setup()
    number = await s.issue()
    await s.run(CloseIssue(repo=REPO_NAME, issue=on(number), reason=reason))
    closed = s.codehost.issues[(REPO.key, number)]
    assert (closed.state, closed.close_reason) == (IssueState.CLOSED, reason)
    await s.run(ReopenIssue(repo=REPO_NAME, issue=on(number)))
    assert s.codehost.issues[(REPO.key, number)].state == IssueState.OPEN


async def test_every_step_is_audited_for_the_asker_and_the_confirmer() -> None:
    s = await setup()
    await s.run(CreateIssue(repo=REPO_NAME, title="Fix it"))
    [entry] = s.audit.entries
    assert entry.result == AuditResult.EXECUTED
    assert entry.action == "create_issue"
    assert entry.actor_member_id == s.franck.id
    assert entry.confirmed_by_member_id == s.annie.id
    assert entry.payload["repo"] == REPO_NAME
    assert entry.payload["number"] == 1


async def test_it_stops_at_the_first_failure_and_says_what_ran() -> None:
    s = await setup()
    number = await s.issue()
    done, problem = await s.run(
        AddLabels(repo=REPO_NAME, issue=on(number), labels=("bug",)),
        CloseIssue(repo=REPO_NAME, issue=on(999)),  # not there
        CommentOnIssue(repo=REPO_NAME, issue=on(number), body="never"),
    )
    assert done == [
        messages.DONE_ADDED_LABELS.format(labels="bug", issue=f'#{number} "Checkout crashes"')
    ]
    assert problem == messages.STEP_NOT_FOUND
    assert s.codehost.comments == []
    assert [e.result for e in s.audit.entries] == [AuditResult.EXECUTED, AuditResult.FAILED]


async def test_the_asker_loses_the_permission_between_asking_and_confirming() -> None:
    s = await setup()
    number = await s.issue()
    s.codehost.permissions[(REPO.key, "franck-gh")] = RepoPermission.READ
    done, problem = await s.run(CloseIssue(repo=REPO_NAME, issue=on(number)))
    assert done == []
    assert problem == messages.STEP_NO_LONGER_ALLOWED.format(name="Franck")
    assert s.codehost.issues[(REPO.key, number)].state == IssueState.OPEN
    assert s.audit.entries[-1].result == AuditResult.DENIED


async def test_an_unverified_asker_runs_nothing() -> None:
    s = await setup()
    number = await s.issue()
    s.franck = s.franck.model_copy(update={"codehost": None})
    _, problem = await s.run(CloseIssue(repo=REPO_NAME, issue=on(number)))
    assert problem == messages.STEP_NO_LONGER_ALLOWED.format(name="Franck")


async def test_the_write_limit_stops_a_run() -> None:
    s = await setup()
    number = await s.issue()
    limits = RateLimiter(FakeCounters(), Limits(0, 0, 0, 1))
    s.services = replace(s.services, limits=limits)
    done, problem = await s.run(
        AddLabels(repo=REPO_NAME, issue=on(number), labels=("bug",)),
        CloseIssue(repo=REPO_NAME, issue=on(number)),
    )
    assert len(done) == 1
    assert problem is not None and "too many changes" in problem
    assert s.codehost.issues[(REPO.key, number)].state == IssueState.OPEN


async def test_a_stored_step_that_no_longer_validates_stops_the_run() -> None:
    s = await setup()
    broken = Step(
        action="close_issue", provider="github", proposal={"repo": REPO_NAME, "issue": {}}
    )
    outcome = await execute(
        [broken],
        requester=s.franck,
        confirmer=s.annie,
        services=s.services,
        statuses=StatusLabels(),
    )
    assert outcome.done == []
    assert outcome.problem == messages.STEP_BROKEN


async def test_a_repo_unlinked_since_the_card_was_posted_is_not_written_to() -> None:
    s = await setup()
    workspace = await s.workspaces.for_chat(CHAT)
    assert workspace is not None
    await s.workspaces.unlink_repo(workspace.id, REPO)
    done, problem = await s.run(CreateIssue(repo=REPO_NAME, title="Fix it"))
    assert (done, problem) == ([], messages.STEP_NOT_LINKED)
    assert s.codehost.issues == {}
    assert s.audit.entries[-1].result == AuditResult.DENIED


async def test_an_unexpected_error_keeps_the_earlier_steps_and_audits_the_failure() -> None:
    s = await setup()
    number = await s.issue()

    async def boom(*_: object, **__: object) -> None:
        raise RuntimeError("boom")

    s.codehost.update_issue = boom  # type: ignore[method-assign]
    done, problem = await s.run(
        CreateIssue(repo=REPO_NAME, title="First"),
        CloseIssue(repo=REPO_NAME, issue=on(number)),
    )
    assert len(done) == 1 and done[0].startswith("Created #")
    assert problem == messages.STEP_CRASHED
    assert [e.result for e in s.audit.entries] == [AuditResult.EXECUTED, AuditResult.FAILED]


# ----------------------------------------------------- what label steps changed, and undoing it


async def test_a_status_step_records_what_it_added_and_removed() -> None:
    s = await setup()
    number = await s.issue(labels=("bug", "status: todo"))
    out = await s.outcome(SetStatus(repo=REPO_NAME, issue=on(number), status="status: review"))
    assert out.effects == [
        {
            "provider": "github",
            "repo": REPO_NAME,
            "number": number,
            "added": ["status: review"],
            "removed": ["status: todo"],
            "statuses": ["status: todo", "status: in progress", "status: review", "status: done"],
        }
    ]


async def test_only_labels_that_really_changed_are_recorded() -> None:
    s = await setup()
    number = await s.issue(labels=("bug",))
    added = await s.outcome(AddLabels(repo=REPO_NAME, issue=on(number), labels=("BUG", "urgent")))
    assert added.effects[0]["added"] == ["urgent"] and added.effects[0]["removed"] == []
    removed = await s.outcome(
        RemoveLabels(repo=REPO_NAME, issue=on(number), labels=("urgent", "missing"))
    )
    assert removed.effects[0]["removed"] == ["urgent"] and removed.effects[0]["added"] == []


async def test_a_step_that_changes_nothing_records_nothing() -> None:
    s = await setup()
    number = await s.issue(labels=("bug",))
    out = await s.outcome(AddLabels(repo=REPO_NAME, issue=on(number), labels=("bug",)))
    assert out.effects == []


async def test_a_run_nobody_confirmed_is_audited_without_a_confirmer() -> None:
    s = await setup()
    number = await s.issue()
    await s.outcome(AddLabels(repo=REPO_NAME, issue=on(number), labels=("bug",)))
    assert s.audit.entries[-1].confirmed_by_member_id is None


async def test_effects_survive_a_later_step_that_fails() -> None:
    s = await setup()
    number = await s.issue()
    out = await s.outcome(
        AddLabels(repo=REPO_NAME, issue=on(number), labels=("bug",)),
        CloseIssue(repo=REPO_NAME, issue=on(999)),
    )
    assert out.problem is not None and len(out.effects) == 1  # what ran can still be undone


async def test_undoing_a_status_change_puts_the_old_status_back() -> None:
    s = await setup()
    number = await s.issue(labels=("bug", "status: todo"))
    out = await s.outcome(SetStatus(repo=REPO_NAME, issue=on(number), status="status: review"))
    result = await undo(out.effects, requester=s.franck, confirmer=s.annie, services=s.services)
    assert result.problem is None
    assert set(s.codehost.issues[(REPO.key, number)].labels) == {"bug", "status: todo"}
    assert result.done == [messages.DONE_UNDO_LABELS.format(issue=f'#{number} "Checkout crashes"')]


async def test_undo_leaves_labels_somebody_else_changed_alone() -> None:
    s = await setup()
    number = await s.issue(labels=("bug",))
    out = await s.outcome(AddLabels(repo=REPO_NAME, issue=on(number), labels=("urgent",)))
    current = s.codehost.issues[(REPO.key, number)]
    s.codehost.issues[(REPO.key, number)] = current.model_copy(
        update={"labels": (*current.labels, "wontfix")}
    )  # a person adds one on GitHub meanwhile
    await undo(out.effects, requester=s.franck, confirmer=s.franck, services=s.services)
    assert set(s.codehost.issues[(REPO.key, number)].labels) == {"bug", "wontfix"}


async def test_undoing_a_removal_puts_the_label_back_once() -> None:
    s = await setup()
    number = await s.issue(labels=("bug", "urgent"))
    out = await s.outcome(RemoveLabels(repo=REPO_NAME, issue=on(number), labels=("urgent",)))
    await undo(out.effects, requester=s.franck, confirmer=s.franck, services=s.services)
    await undo(out.effects, requester=s.franck, confirmer=s.franck, services=s.services)
    assert sorted(s.codehost.issues[(REPO.key, number)].labels) == ["bug", "urgent"]


async def test_undo_is_checked_and_audited_like_any_change() -> None:
    s = await setup()
    number = await s.issue(labels=("bug",))
    out = await s.outcome(AddLabels(repo=REPO_NAME, issue=on(number), labels=("urgent",)))
    s.codehost.permissions[(REPO.key, "franck-gh")] = RepoPermission.READ
    s.services = replace(s.services, permissions=PermissionChecker(s.codehost))
    result = await undo(out.effects, requester=s.franck, confirmer=s.annie, services=s.services)
    assert result.problem == messages.STEP_NO_LONGER_ALLOWED.format(name="Franck")
    assert "urgent" in s.codehost.issues[(REPO.key, number)].labels  # nothing was touched
    entry = s.audit.entries[-1]
    assert (entry.action, entry.result) == ("undo_labels", AuditResult.DENIED)
    assert entry.confirmed_by_member_id == s.annie.id


async def test_undoing_on_an_issue_that_is_gone_says_so() -> None:
    s = await setup()
    number = await s.issue()
    out = await s.outcome(AddLabels(repo=REPO_NAME, issue=on(number), labels=("bug",)))
    del s.codehost.issues[(REPO.key, number)]
    result = await undo(out.effects, requester=s.franck, confirmer=s.franck, services=s.services)
    assert result.problem == messages.STEP_NOT_FOUND


async def test_undoing_several_changes_to_one_issue_goes_last_to_first() -> None:
    s = await setup()
    number = await s.issue(labels=("bug", "status: todo"))
    out = await s.outcome(
        SetStatus(repo=REPO_NAME, issue=on(number), status="status: in progress"),
        SetStatus(repo=REPO_NAME, issue=on(number), status="status: review"),
    )
    await undo(out.effects, requester=s.franck, confirmer=s.franck, services=s.services)
    assert set(s.codehost.issues[(REPO.key, number)].labels) == {"bug", "status: todo"}


async def test_undoing_an_add_followed_by_a_remove_of_the_same_label() -> None:
    s = await setup()
    number = await s.issue(labels=("bug",))
    out = await s.outcome(
        AddLabels(repo=REPO_NAME, issue=on(number), labels=("urgent",)),
        RemoveLabels(repo=REPO_NAME, issue=on(number), labels=("urgent",)),
    )
    await undo(out.effects, requester=s.franck, confirmer=s.franck, services=s.services)
    assert set(s.codehost.issues[(REPO.key, number)].labels) == {"bug"}  # as it started


async def test_undo_does_not_bring_back_a_status_somebody_replaced_since() -> None:
    s = await setup()
    number = await s.issue(labels=("bug", "status: todo"))
    out = await s.outcome(SetStatus(repo=REPO_NAME, issue=on(number), status="status: review"))
    current = s.codehost.issues[(REPO.key, number)]
    s.codehost.issues[(REPO.key, number)] = current.model_copy(
        update={"labels": ("bug", "status: done")}  # a person moved it on GitHub meanwhile
    )
    await undo(out.effects, requester=s.franck, confirmer=s.franck, services=s.services)
    assert set(s.codehost.issues[(REPO.key, number)].labels) == {"bug", "status: done"}


async def test_an_effect_stored_before_status_labels_were_recorded_still_undoes() -> None:
    s = await setup()
    number = await s.issue(labels=("bug", "status: done"))
    legacy = {
        "provider": "github",
        "repo": REPO_NAME,
        "number": number,
        "added": ["status: review"],
        "removed": ["status: todo"],
    }  # no "statuses": the `status: ` labels count, and one is there now
    await undo([legacy], requester=s.franck, confirmer=s.franck, services=s.services)
    assert set(s.codehost.issues[(REPO.key, number)].labels) == {"bug", "status: done"}


async def test_audit_entries_say_which_stored_proposal_they_belong_to() -> None:
    from uuid import uuid4

    s = await setup()
    number = await s.issue()
    pending_id = uuid4()
    out = await execute(
        [Step.of(AddLabels(repo=REPO_NAME, issue=on(number), labels=("bug",)), REPO)],
        requester=s.franck,
        confirmer=None,
        services=s.services,
        statuses=StatusLabels(),
        pending_id=pending_id,
    )
    await undo(
        out.effects,
        requester=s.franck,
        confirmer=s.franck,
        services=s.services,
        pending_id=pending_id,
    )
    assert [e.payload["pending_id"] for e in s.audit.entries[-2:]] == [str(pending_id)] * 2
