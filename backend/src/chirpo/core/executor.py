"""Runs the stored steps of a confirmed proposal on the code host (M5.5, D53).

Everything runs as the person who asked: their permissions are checked again now, since a
confirmation can come long after the request, and nobody can do through chirpo more than the code
host lets them do directly. Steps run in order and the run stops at the first one that can't:
a half-applied request is easier to untangle when it is told exactly where it stopped.
Text written by the model is cleaned before it is posted (core/outbound.py).
"""

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

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
from chirpo.core.context import MAX_NAME
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
from chirpo.core.outbound import neutralise, shown
from chirpo.core.pending import Step
from chirpo.core.permissions import Action
from chirpo.core.statuses import COLORS, DEFAULT_PREFIX, StatusLabels, clears, shown_status
from chirpo.core.workspaces import AuditEntry, Member

log = logging.getLogger(__name__)

MAX_TITLE = 256  # GitHub's limit; cleaning can add characters


@dataclass(frozen=True, slots=True)
class Outcome:
    done: list[str]  # one result text per thing that ran
    problem: str | None = None  # why it stopped, when it did
    stopped_at: int | None = None  # index of the step that stopped it
    # What label steps changed (labels added and removed), so a person can undo them.
    effects: list[dict[str, Any]] = field(default_factory=list)


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
    effects: list[dict[str, Any]] = field(default_factory=list)


async def execute(
    steps: Sequence[Step],
    *,
    requester: Member,
    confirmer: Member | None,
    services: Services,
    statuses: StatusLabels,
    pending_id: UUID | None = None,
) -> Outcome:
    """Runs the steps in order for `requester`. `confirmer` is who pressed Confirm, or None when
    nobody had to (status and label changes the sender named, D56). `statuses`: the workspace's
    status labels as they are now."""
    done: list[str] = []
    effects: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        run = _Run()
        payload: dict[str, Any] = _of(pending_id)

        async def work(
            step: Step = step, run: _Run = run, payload: dict[str, Any] = payload
        ) -> None:
            repo, proposal = _load(step)
            payload["repo"] = repo.full_name
            await _guard(proposal, repo, requester, services)
            await _run(proposal, repo, requester, services, run, statuses)

        result, error, reason = await _attempt(step.action, work)
        done += run.lines
        effects += run.effects
        await _audit(
            services, requester, confirmer, step.action, result, {**payload, **run.detail}, error
        )
        if reason is not None:
            log.info("step_stopped action=%s result=%s", step.action, result)
            return Outcome(done, reason, index, effects)
    return Outcome(done, effects=effects)


async def undo(
    effects: Sequence[Mapping[str, Any]],
    *,
    requester: Member,
    confirmer: Member,
    services: Services,
    pending_id: UUID | None = None,
) -> Outcome:
    """Reverses recorded label changes on the issues' current labels: what was added comes off,
    what was removed goes back, and anything else somebody changed since stays. As the asker."""
    done: list[str] = []
    # Last change first: each one is undone on top of the ones that came after it.
    for index, effect in enumerate(reversed(effects)):
        payload = {**_of(pending_id), "repo": effect["repo"], "number": effect["number"]}

        async def work(effect: Mapping[str, Any] = effect) -> None:
            repo = RepoRef.parse(effect["provider"], effect["repo"])
            await _check(Action.TRIAGE_ISSUE, repo, requester, services)
            issue = await services.codehost.get_issue(repo, effect["number"])
            current = _without(issue.labels, effect["added"])
            restore = effect["removed"]
            is_status = _status_test(effect)
            if any(is_status(label) for label in current):
                # Somebody set another status since: putting the old one back would leave two.
                restore = [label for label in restore if not is_status(label)]
            labels = _union(current, restore)
            await services.codehost.update_issue(repo, issue.number, IssuePatch(labels=labels))
            done.append(messages.DONE_UNDO_LABELS.format(issue=_ref(issue)))

        result, error, reason = await _attempt("undo_labels", work)
        await _audit(services, requester, confirmer, "undo_labels", result, payload, error)
        if reason is not None:
            return Outcome(done, reason, index)
    return Outcome(done)


async def _attempt(
    action: str, work: Callable[[], Awaitable[None]]
) -> tuple[AuditResult, str | None, str | None]:
    """Runs `work` and says how it ended: (audit result, error code, what to tell people)."""
    try:
        await work()
    except _Stop as stop:
        return stop.result, stop.code, stop.reason
    except CodeHostNotFoundError as exc:
        return AuditResult.FAILED, type(exc).__name__, messages.STEP_NOT_FOUND
    except CodeHostError as exc:
        error = type(exc).__name__
        return AuditResult.FAILED, error, messages.STEP_REFUSED.format(reason=error)
    except Exception as exc:
        # Whatever else: what ran before (and this step's own audit) must not be lost, or the
        # person asks again and gets a duplicate. Cancellation is not an Exception: it passes.
        log.warning("step_crashed action=%s error=%s", action, type(exc).__name__, exc_info=True)
        return AuditResult.FAILED, type(exc).__name__, messages.STEP_CRASHED
    return AuditResult.EXECUTED, None, None


async def _audit(
    services: Services,
    requester: Member,
    confirmer: Member | None,
    action: str,
    result: AuditResult,
    payload: dict[str, Any],
    error: str | None,
) -> None:
    await services.audit.record(
        AuditEntry(
            workspace_id=requester.workspace_id,
            actor_member_id=requester.id,
            confirmed_by_member_id=confirmer.id if confirmer else None,
            action=action,
            result=result,
            payload=payload,
            error=error,
        )
    )


def _of(pending_id: UUID | None) -> dict[str, Any]:
    """Which stored proposal an audit entry belongs to, when there is one."""
    return {"pending_id": str(pending_id)} if pending_id else {}


def _load(step: Step) -> tuple[RepoRef, Proposal]:
    try:
        return step.load()
    except (ValueError, KeyError):  # ValueError includes pydantic's ValidationError
        raise _Stop(messages.STEP_BROKEN, code="invalid_step") from None


async def _guard(proposal: Proposal, repo: RepoRef, requester: Member, services: Services) -> None:
    action = permission_of(proposal)
    if action is None:
        raise _Stop(messages.STEP_BROKEN, code="invalid_step")
    await _check(action, repo, requester, services)


async def _check(action: Action, repo: RepoRef, requester: Member, services: Services) -> None:
    """The asker's permission, the repo still being linked, and a write allowance, as of now."""
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
        name = shown(requester.display_name, MAX_NAME)
        text = messages.STEP_NO_LONGER_ALLOWED.format(name=name)
        raise _Stop(text, code="not_allowed", result=AuditResult.DENIED)
    if services.limits is not None:
        limit = await services.limits.check_write(requester.id)
        if not limit.allowed:
            wait = messages.duration(limit.retry_after)
            text = messages.STEP_WRITE_LIMITED.format(wait=wait)
            raise _Stop(text, code="write_limited", result=AuditResult.DENIED)


async def _run(
    proposal: Proposal,
    repo: RepoRef,
    requester: Member,
    services: Services,
    run: _Run,
    statuses: StatusLabels,
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
            after = await _set_status(proposal.status, issue, repo, services, statuses)
            _record(run, repo, issue, after, statuses)
            status = shown(shown_status(proposal.status), MAX_NAME)
            run.lines.append(messages.DONE_STATUS.format(issue=_ref(issue), status=status))
        case AddLabels():
            issue = await _issue(proposal, repo, services)
            labels = _union(issue.labels, proposal.labels)
            await host.update_issue(repo, issue.number, IssuePatch(labels=labels))
            _record(run, repo, issue, labels, statuses)
            run.lines.append(
                messages.DONE_ADDED_LABELS.format(
                    labels=_labels(proposal.labels), issue=_ref(issue)
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
            _record(run, repo, issue, kept, statuses)
            run.lines.append(
                messages.DONE_REMOVED_LABELS.format(
                    labels=_labels(proposal.labels), issue=_ref(issue)
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


async def _set_status(
    label: str, issue: Issue, repo: RepoRef, services: Services, statuses: StatusLabels
) -> tuple[str, ...]:
    """Makes `label` the issue's only status label; returns the labels it now has. A label that
    isn't one of the workspace's (renamed since the card was posted) stops the step."""
    status = statuses.status_of(label)
    if status is None:
        raise _Stop(messages.STATUS_LABELS_CHANGED, code="status_labels_changed")
    label = statuses.label(status)
    await services.codehost.ensure_labels(repo, [LabelSpec(name=label, color=COLORS[status])])
    cleared = {name.casefold() for name in clears(statuses)}
    others = tuple(name for name in issue.labels if name.casefold() not in cleared)
    labels = (*others, label)
    await services.codehost.update_issue(repo, issue.number, IssuePatch(labels=labels))
    return labels


def _record(
    run: _Run,
    repo: RepoRef,
    issue: Issue,
    after: Sequence[str],
    statuses: StatusLabels,
) -> None:
    """What this step did to the labels, for Undo; nothing if it changed nothing. It also keeps
    which labels were statuses then, so Undo still knows after a rename."""
    added = [label for label in after if not _has(issue.labels, label)]
    removed = [label for label in issue.labels if not _has(after, label)]
    if added or removed:
        effect: dict[str, Any] = {
            "provider": repo.provider,
            "repo": repo.full_name,
            "number": issue.number,
            "added": added,
            "removed": removed,
            "statuses": clears(statuses),
        }
        run.effects.append(effect)


def _status_test(effect: Mapping[str, Any]) -> Callable[[str], bool]:
    """Which labels were statuses when the change ran; effects from before M6.4 don't say, and
    statuses were then the `status: ` labels."""
    named = {str(label).casefold() for label in effect.get("statuses") or ()}
    if named:
        return lambda label: label.casefold() in named
    return lambda label: label.startswith(DEFAULT_PREFIX)


def _has(labels: Sequence[str], label: str) -> bool:
    return any(same_login(label, other) for other in labels)


def _without(current: Sequence[str], gone: Sequence[str]) -> tuple[str, ...]:
    return tuple(label for label in current if not _has(gone, label))


def _with_footer(body: str, login: str) -> str:
    footer = messages.REQUESTED_BY.format(login=login)
    return f"{neutralise(body)}\n\n{footer}" if body.strip() else footer


def _union(current: Sequence[str], added: Sequence[str]) -> tuple[str, ...]:
    result = list(current)
    result += [a for a in added if not any(same_login(a, c) for c in result)]
    return tuple(result)


def _ref(issue: Issue) -> str:
    return messages.DRY_ISSUE.format(number=issue.number, title=shown(issue.title, MAX_NAME))


def _created(issue: Issue) -> str:
    title = shown(issue.title, MAX_TITLE)
    return messages.ISSUE_CREATED.format(number=issue.number, title=title, url=issue.url)


def _labels(names: Sequence[str]) -> str:
    """Label names are outside text (anyone with write access names them): shown, not posted raw."""
    return ", ".join(shown(name, MAX_NAME) for name in names)
