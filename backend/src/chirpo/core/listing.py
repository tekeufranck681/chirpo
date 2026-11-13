"""Read requests ("my issues", "Franck's bugs") run right away: they change nothing (D47)."""

from typing import Literal

from chirpo.core import messages
from chirpo.core.actions import ListIssues, MyIssues
from chirpo.core.context import clean
from chirpo.core.domain import Issue, IssueQuery, IssueState, Milestone
from chirpo.core.ports import CodeHostAdapter
from chirpo.core.resolution import Resolved

MY_ISSUES_LIMIT = 20
MAX_TITLE = 100
_STATES: dict[str, IssueState | Literal["all"]] = {
    "open": IssueState.OPEN,
    "closed": IssueState.CLOSED,
    "all": "all",
}


def is_read(step: Resolved) -> bool:
    return isinstance(step.proposal, ListIssues | MyIssues)


async def run_read(step: Resolved, codehost: CodeHostAdapter) -> str:
    proposal, repo = step.proposal, step.repo
    if isinstance(proposal, MyIssues):
        me = step.login
        query = IssueQuery(
            state=_STATES[proposal.state],
            assignee=me,
            milestone=step.milestone.number if step.milestone else None,
            limit=MY_ISSUES_LIMIT,
        )
        filters = messages.FILTER_YOURS
    elif isinstance(proposal, ListIssues):
        me = None
        query = IssueQuery(
            state=_STATES[proposal.state],
            assignee=proposal.assignee,
            labels=proposal.labels,
            milestone=step.milestone.number if step.milestone else None,
            limit=proposal.limit,
        )
        filters = ""
        if proposal.assignee:
            filters += messages.DRY_FILTER_ASSIGNEE.format(login=proposal.assignee)
        if proposal.labels:
            filters += messages.DRY_FILTER_LABELS.format(labels=", ".join(proposal.labels))
    else:
        raise TypeError(f"{type(proposal).__name__} is not a read")
    if step.milestone:
        filters += _milestone_filter(step.milestone)
    issues = await codehost.list_issues(repo, query)
    if not issues:
        return messages.ISSUES_NONE.format(
            state=proposal.state, repo=repo.full_name, filters=filters
        )
    title = messages.ISSUES_TITLE.format(
        state=proposal.state.capitalize(), repo=repo.full_name, filters=filters
    )
    return "\n".join([title, *(_item(issue, me) for issue in issues)])


def _item(issue: Issue, me: str | None) -> str:
    title = clean(issue.title, MAX_TITLE)
    if me is None:
        people = messages.ISSUE_PEOPLE.format(people=", ".join(issue.assignees))
    else:
        others = [login for login in issue.assignees if login.casefold() != me.casefold()]
        people = messages.ISSUE_ALSO.format(people=", ".join(others)) if others else ""
    if me is None and not issue.assignees:
        people = ""
    return messages.ISSUE_ITEM.format(number=issue.number, title=title, people=people)


def _milestone_filter(milestone: Milestone) -> str:
    text = messages.DRY_FILTER_MILESTONE.format(title=clean(milestone.title, MAX_TITLE))
    if milestone.due_on:
        due = milestone.due_on
        text += messages.DRY_FILTER_DUE.format(day=due.day, month=messages.MONTHS[due.month - 1])
    return text
