"""The reply that says what chirpo would do with changes (M4.8), before confirming and running
arrive in M5. Reads run right away instead (core/listing.py)."""

from collections.abc import Sequence

from chirpo.core import messages
from chirpo.core.actions import (
    AddLabels,
    AssignIssue,
    CloseIssue,
    CommentOnIssue,
    CreateIssue,
    CreateIssues,
    IssueDraft,
    RemoveLabels,
    ReopenIssue,
    SetStatus,
    UnassignIssue,
)
from chirpo.core.context import clean
from chirpo.core.domain import RepoRef
from chirpo.core.resolution import Resolved

MAX_SHOWN = 120  # characters of a title, body or comment in the reply


def describe(steps: Sequence[Resolved], default_repo: RepoRef) -> str:
    lines = [messages.DRY_RUN_HEADER]
    for step in steps:
        lines += _lines(step, default_repo)
        lines += [messages.DRY_NOTE.format(note=note) for note in step.notes]
    return "\n".join(lines)


def _lines(step: Resolved, default_repo: RepoRef) -> list[str]:
    proposal, repo = step.proposal, step.repo.full_name
    issue = _issue(step, default_repo)
    match proposal:
        case CreateIssue():
            return _draft(proposal, repo)
        case CreateIssues():
            return [line for draft in proposal.issues for line in _draft(draft, repo)]
        case AssignIssue():
            return [messages.DRY_ASSIGN.format(people=_people(proposal.assignees), issue=issue)]
        case UnassignIssue():
            return [messages.DRY_UNASSIGN.format(people=_people(proposal.assignees), issue=issue)]
        case SetStatus():
            status = proposal.status.removeprefix("status: ")
            return [messages.DRY_STATUS.format(issue=issue, status=status)]
        case AddLabels():
            return [messages.DRY_ADD_LABELS.format(labels=", ".join(proposal.labels), issue=issue)]
        case RemoveLabels():
            labels = ", ".join(proposal.labels)
            return [messages.DRY_REMOVE_LABELS.format(labels=labels, issue=issue)]
        case CommentOnIssue():
            return [messages.DRY_COMMENT.format(issue=issue, text=_short(proposal.body))]
        case CloseIssue():
            reason = messages.CLOSE_REASONS[proposal.reason.value]
            return [messages.DRY_CLOSE.format(issue=issue, reason=reason)]
        case ReopenIssue():
            return [messages.DRY_REOPEN.format(issue=issue)]
    raise TypeError(f"no dry-run text for {type(proposal).__name__}")


def _draft(draft: IssueDraft, repo: str) -> list[str]:
    lines = [messages.DRY_CREATE.format(repo=repo, title=_short(draft.title))]
    if draft.assignees:
        lines.append(messages.DRY_ASSIGNED.format(people=_people(draft.assignees)))
    if draft.labels:
        lines.append(messages.DRY_LABELS.format(labels=", ".join(draft.labels)))
    if draft.body:
        lines.append(messages.DRY_DETAILS.format(text=_short(draft.body)))
    return lines


def _issue(step: Resolved, default_repo: RepoRef) -> str:
    if step.issue is None:
        return ""
    text = messages.DRY_ISSUE.format(number=step.issue.number, title=_short(step.issue.title))
    if step.repo != default_repo:
        text += messages.DRY_IN_REPO.format(repo=step.repo.full_name)
    return text


def _people(logins: Sequence[str]) -> str:
    # No "@": Telegram would turn a GitHub login into a mention of whoever has it as a username.
    return ", ".join(logins)


def _short(text: str) -> str:
    """Untrusted text (titles, bodies) as one visible line, cut to MAX_SHOWN."""
    line = clean(text, MAX_SHOWN + 1)
    return line if len(line) <= MAX_SHOWN else line[: MAX_SHOWN - 1] + "…"
