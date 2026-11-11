"""Runs the stored steps of a confirmed proposal on the code host (M5.5, D53).

Everything runs as the person who asked: their permissions are checked again now, since a
confirmation can come long after the request, and nobody can do through chirpo more than the code
host lets them do directly. Steps run in order and the run stops at the first one that can't:
a half-applied request is easier to untangle when it is told exactly where it stopped.
Text written by the model is cleaned before it is posted (core/outbound.py).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from chirpo.core import messages
from chirpo.core.actions import (
    AddLabels,
    AssignIssue,
    CloseIssue,
    CommentOnIssue,
    CreateIssue,
    CreateIssues,
    IssueDraft,
    Proposal,
    RemoveLabels,
    ReopenIssue,
    SetStatus,
    UnassignIssue,
    permission_of,
)
from chirpo.core.commands.base import Services
from chirpo.core.context import MAX_NAME, clean
from chirpo.core.domain import (
    AuditResult,
    Issue,
    IssuePatch,
    IssueState,
    LabelSpec,
    NewIssue,
    RepoRef,
    same_login,
)
from chirpo.core.errors import CodeHostError, CodeHostNotFoundError
from chirpo.core.outbound import neutralise
from chirpo.core.pending import Step
from chirpo.core.resolution import STATUS_LABELS
from chirpo.core.workspaces import AuditEntry, Member

log = logging.getLogger(__name__)

MAX_TITLE = 256  # GitHub's limit; cleaning can add characters
STATUS_PREFIX = "status: "
STATUS_COLORS = {
    "status: todo": "ededed",
    "status: in progress": "fbca04",
    "status: review": "5319e7",
    "status: done": "0e8a16",
}


@dataclass(frozen=True, slots=True)
class Outcome:
    done: list[str]  # one result text per thing that ran
    problem: str | None = None  # why it stopped, when it did
    stopped_at: int | None = None  # index of the step that stopped it


class _Stop(Exception):
    """A step that can't run. `code` is what the audit log records, `reason` what people read."""

    def __init__(self, reason: str, *, code: str, result: AuditResult = AuditResult.FAILED) -> None:
        super().__init__(code)
        self.reason = reason
        self.code = code
        self.result = result


@dataclass
class _Run:
    """What one step did so far, kept even when it fails halfway (several issues)."""

    lines: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


async def execute(
    steps: Sequence[Step], *, requester: Member, confirmer: Member, services: Services
) -> Outcome:
    done: list[str] = []
    for index, step in enumerate(steps):
        run = _Run()
        result, error, reason = AuditResult.EXECUTED, None, None
        payload: dict[str, Any] = {}
        try:
            repo, proposal = _load(step)
            payload = {"repo": repo.full_name}
            await _guard(proposal, repo, requester, services)
            await _run(proposal, repo, requester, services, run)
        except _Stop as stop:
            result, error, reason = stop.result, stop.code, stop.reason
        except CodeHostNotFoundError as exc:
            result, error, reason = AuditResult.FAILED, type(exc).__name__, messages.STEP_NOT_FOUND
        except CodeHostError as exc:
            result, error = AuditResult.FAILED, type(exc).__name__
            reason = messages.STEP_REFUSED.format(reason=error)
        except Exception as exc:
            # Whatever else: what ran before (and this step's own audit) must not be lost, or the
            # person asks again and gets a duplicate. Cancellation is not an Exception: it passes.
            log.warning(
                "step_crashed action=%s error=%s", step.action, type(exc).__name__, exc_info=True
            )
            result, error, reason = AuditResult.FAILED, type(exc).__name__, messages.STEP_CRASHED
        done += run.lines
        await services.audit.record(
            AuditEntry(
                workspace_id=requester.workspace_id,
                actor_member_id=requester.id,
                confirmed_by_member_id=confirmer.id,
                action=step.action,
                result=result,
                payload={**payload, **run.detail},
                error=error,
            )
        )
        if reason is not None:
            log.info("step_stopped action=%s result=%s", step.action, result)
            return Outcome(done, reason, index)
    return Outcome(done)


def _load(step: Step) -> tuple[RepoRef, Proposal]:
    try:
        return step.load()
    except (ValueError, KeyError):  # ValueError includes pydantic's ValidationError
        raise _Stop(messages.STEP_BROKEN, code="invalid_step") from None


async def _guard(proposal: Proposal, repo: RepoRef, requester: Member, services: Services) -> None:
    """The asker's permission and write allowance, as of now."""
    action = permission_of(proposal)
    if action is None:
        raise _Stop(messages.STEP_BROKEN, code="invalid_step")
    try:
        verdict = await services.permissions.check(requester, action, repo)
    except CodeHostError:
        raise _Stop(messages.STEP_REFUSED.format(reason="no access"), code="no_access") from None
    if not any(
        link.repo.key == repo.key
        for link in await services.workspaces.repos(requester.workspace_id)
    ):
        raise _Stop(messages.STEP_NOT_LINKED, code="not_linked", result=AuditResult.DENIED)
    if not verdict.allowed:
        name = clean(requester.display_name, MAX_NAME)
        text = messages.STEP_NO_LONGER_ALLOWED.format(name=name)
        raise _Stop(text, code="not_allowed", result=AuditResult.DENIED)
    if services.limits is not None:
        limit = await services.limits.check_write(requester.id)
        if not limit.allowed:
            wait = messages.duration(limit.retry_after)
            text = messages.STEP_WRITE_LIMITED.format(wait=wait)
            raise _Stop(text, code="write_limited", result=AuditResult.DENIED)


async def _run(
    proposal: Proposal, repo: RepoRef, requester: Member, services: Services, run: _Run
) -> None:
    host = services.codehost
    login = requester.codehost.login if requester.codehost else "?"
    match proposal:
        case CreateIssue():
            created = await _create(proposal, repo, login, services)
            run.lines.append(_created(created))
            run.detail["number"] = created.number
        case CreateIssues():
            run.detail["numbers"] = []
            for draft in proposal.issues:
                created = await _create(draft, repo, login, services)
                run.lines.append(_created(created))
                run.detail["numbers"].append(created.number)
        case AssignIssue():
            issue = await _issue(proposal, repo, services)
            people = _union(issue.assignees, proposal.assignees)
            await host.update_issue(repo, issue.number, IssuePatch(assignees=people))
            run.lines.append(
                messages.DONE_ASSIGNED.format(
                    people=", ".join(proposal.assignees), issue=_ref(issue)
                )
            )
        case UnassignIssue():
            issue = await _issue(proposal, repo, services)
            kept = tuple(
                a for a in issue.assignees if not any(same_login(a, r) for r in proposal.assignees)
            )
            await host.update_issue(repo, issue.number, IssuePatch(assignees=kept))
            run.lines.append(
                messages.DONE_UNASSIGNED.format(
                    people=", ".join(proposal.assignees), issue=_ref(issue)
                )
            )
        case SetStatus():
            issue = await _issue(proposal, repo, services)
            await _set_status(proposal.status, issue, repo, services)
            status = proposal.status.removeprefix(STATUS_PREFIX)
            run.lines.append(messages.DONE_STATUS.format(issue=_ref(issue), status=status))
        case AddLabels():
            issue = await _issue(proposal, repo, services)
            labels = _union(issue.labels, proposal.labels)
            await host.update_issue(repo, issue.number, IssuePatch(labels=labels))
            run.lines.append(
                messages.DONE_ADDED_LABELS.format(
                    labels=", ".join(proposal.labels), issue=_ref(issue)
                )
            )
        case RemoveLabels():
            issue = await _issue(proposal, repo, services)
            kept = tuple(
                label
                for label in issue.labels
                if not any(same_login(label, r) for r in proposal.labels)
            )
            await host.update_issue(repo, issue.number, IssuePatch(labels=kept))
            run.lines.append(
                messages.DONE_REMOVED_LABELS.format(
                    labels=", ".join(proposal.labels), issue=_ref(issue)
                )
            )
        case CommentOnIssue():
            issue = await _issue(proposal, repo, services)
            text = _with_footer(proposal.body, login)
            url = await host.add_comment(repo, issue.number, text)
            run.lines.append(messages.DONE_COMMENTED.format(issue=_ref(issue), url=url))
        case CloseIssue():
            issue = await _issue(proposal, repo, services)
            patch = IssuePatch(state=IssueState.CLOSED, close_reason=proposal.reason)
            await host.update_issue(repo, issue.number, patch)
            reason = messages.CLOSE_REASONS[proposal.reason.value]
            run.lines.append(messages.DONE_CLOSED.format(issue=_ref(issue), reason=reason))
        case ReopenIssue():
            issue = await _issue(proposal, repo, services)
            await host.update_issue(repo, issue.number, IssuePatch(state=IssueState.OPEN))
            run.lines.append(messages.DONE_REOPENED.format(issue=_ref(issue)))
        case _:
            raise _Stop(messages.STEP_BROKEN, code="invalid_step")


async def _create(draft: IssueDraft, repo: RepoRef, login: str, services: Services) -> Issue:
    new = NewIssue(
        title=neutralise(draft.title)[:MAX_TITLE],
        body=_with_footer(draft.body, login),
        assignees=draft.assignees,
        labels=draft.labels,
    )
    return await services.codehost.create_issue(repo, new)


async def _issue(proposal: Any, repo: RepoRef, services: Services) -> Issue:
    number = proposal.issue.number
    if number is None:  # resolution always gives a number: anything else was tampered with
        raise _Stop(messages.STEP_BROKEN, code="invalid_step")
    return await services.codehost.get_issue(repo, number)


async def _set_status(status: str, issue: Issue, repo: RepoRef, services: Services) -> None:
    if status not in STATUS_LABELS.values():
        raise _Stop(messages.STEP_BROKEN, code="invalid_step")
    await services.codehost.ensure_labels(
        repo, [LabelSpec(name=status, color=STATUS_COLORS[status])]
    )
    others = tuple(label for label in issue.labels if not label.startswith(STATUS_PREFIX))
    await services.codehost.update_issue(repo, issue.number, IssuePatch(labels=(*others, status)))


def _with_footer(body: str, login: str) -> str:
    footer = messages.REQUESTED_BY.format(login=login)
    return f"{neutralise(body)}\n\n{footer}" if body.strip() else footer


def _union(current: Sequence[str], added: Sequence[str]) -> tuple[str, ...]:
    result = list(current)
    result += [a for a in added if not any(same_login(a, c) for c in result)]
    return tuple(result)


def _ref(issue: Issue) -> str:
    return messages.DRY_ISSUE.format(number=issue.number, title=clean(issue.title, MAX_NAME))


def _created(issue: Issue) -> str:
    return messages.ISSUE_CREATED.format(number=issue.number, title=issue.title, url=issue.url)
