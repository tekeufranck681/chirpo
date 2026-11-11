"""The card that says what chirpo will do with changes, shown for confirming (M4.8, M5.2). It shows
the whole resolved payload; a long body or comment is cut with a count of what is hidden. Reads
run right away instead (core/listing.py)."""

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
from chirpo.core.outbound import break_mentions
from chirpo.core.resolution import Resolved

MAX_TITLE = 256  # GitHub's limit: a title is always shown whole
MAX_TEXT = 600  # characters of a body or comment shown on the card


def describe(steps: Sequence[Resolved], default_repo: RepoRef, text_limit: int = MAX_TEXT) -> str:
    lines = [messages.DRY_RUN_HEADER]
    for step in steps:
        lines += _lines(step, default_repo, text_limit)
        lines += [messages.DRY_NOTE.format(note=note) for note in step.notes]
    return "\n".join(lines)


def _lines(step: Resolved, default_repo: RepoRef, limit: int) -> list[str]:
    proposal, repo = step.proposal, step.repo.full_name
    issue = _issue(step, default_repo)
    match proposal:
        case CreateIssue():
            return _draft(proposal, repo, limit)
        case CreateIssues():
            return [line for draft in proposal.issues for line in _draft(draft, repo, limit)]
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
            return [messages.DRY_COMMENT.format(issue=issue, text=_text(proposal.body, limit))]
        case CloseIssue():
            reason = messages.CLOSE_REASONS[proposal.reason.value]
            return [messages.DRY_CLOSE.format(issue=issue, reason=reason)]
        case ReopenIssue():
            return [messages.DRY_REOPEN.format(issue=issue)]
    raise TypeError(f"no dry-run text for {type(proposal).__name__}")


def _draft(draft: IssueDraft, repo: str, limit: int) -> list[str]:
    lines = [
        messages.DRY_CREATE.format(repo=repo, title=break_mentions(clean(draft.title, MAX_TITLE)))
    ]
    if draft.assignees:
        lines.append(messages.DRY_ASSIGNED.format(people=_people(draft.assignees)))
    if draft.labels:
        lines.append(messages.DRY_LABELS.format(labels=", ".join(draft.labels)))
    if draft.body:
        lines.append(messages.DRY_DETAILS.format(text=_text(draft.body, limit)))
    return lines


def _issue(step: Resolved, default_repo: RepoRef) -> str:
    if step.issue is None:
        return ""
    text = messages.DRY_ISSUE.format(
        number=step.issue.number, title=break_mentions(clean(step.issue.title, MAX_TITLE))
    )
    if step.repo != default_repo:
        text += messages.DRY_IN_REPO.format(repo=step.repo.full_name)
    return text


def _people(logins: Sequence[str]) -> str:
    # No "@": Telegram would turn a GitHub login into a mention of whoever has it as a username.
    return ", ".join(logins)


def _text(text: str, limit: int) -> str:
    """Untrusted text as one visible line; what doesn't fit is counted, not silently dropped.
    Mentions are broken: Telegram would notify whoever has that username."""
    line = break_mentions(clean(text, 10_000))
    if len(line) <= limit:
        return line
    return line[:limit] + messages.DRY_MORE.format(count=len(line) - limit)
