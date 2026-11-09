"""What the LLM may propose (D44). Each proposal is a strict Pydantic model; the registry links it
to the permission it needs and whether a person must confirm it before it runs.

People, issues and repos stay exactly as the message named them ("Kevin", "the checkout bug",
"api"): code resolves them against the workspace later, so the model never picks a GitHub account.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from chirpo.core.domain import CloseReason
from chirpo.core.permissions import Action

# Control characters (tab and newline aside), bidi overrides that make text read differently than it
# is, and zero-width characters that make two names look alike. The zero-width joiner stays: emoji.
_UNSAFE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b\u200e\u200f\u202a-\u202e\u2028\u2029\u2066-\u2069\ufeff]"
)


def _text(value: str) -> str:
    value = value.replace("\r\n", "\n")
    if _UNSAFE.search(value):
        raise ValueError("contains control or invisible characters")
    return value


def _line(value: str) -> str:
    value = _text(value)
    if "\n" in value:
        raise ValueError("must be a single line")
    return value


Text = Annotated[str, AfterValidator(_text)]
Line = Annotated[str, AfterValidator(_line)]
Person = Annotated[
    Line,
    StringConstraints(min_length=1, max_length=100),
    Field(description='A person as the message names them: "Kevin", "@kevin_t", or "me".'),
]
Label = Annotated[Line, StringConstraints(min_length=1, max_length=50, pattern=r"^[^,]+$")]
Body = Annotated[Text, StringConstraints(max_length=10_000)]
IssueStateFilter = Literal["open", "closed", "all"]


class Proposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class IssueRef(Proposal):
    """The issue the message talks about: its number when given, otherwise how it was described."""

    number: int | None = Field(
        default=None, ge=1, le=2**31 - 1, description="The issue number, e.g. 12 for #12."
    )
    description: Line | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description='How the message describes the issue, e.g. "the checkout bug".',
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.number is None) == (self.description is None):
            raise ValueError("give either the number or a description")
        return self


class RepoScoped(Proposal):
    repo: Line | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="The repository as the message names it. Leave out for the default one.",
    )


class IssueDraft(Proposal):
    title: Line = Field(min_length=1, max_length=256, description="Short, specific issue title.")
    body: Body = Field(default="", description="Details from the message: steps, context, impact.")
    assignees: tuple[Person, ...] = Field(default=(), max_length=10, description="Who to assign.")
    labels: tuple[Label, ...] = Field(default=(), max_length=10, description="Labels to add.")


class CreateIssue(IssueDraft, RepoScoped):
    pass


class CreateIssues(RepoScoped):
    issues: tuple[IssueDraft, ...] = Field(
        min_length=1, max_length=10, description="One entry per issue asked for."
    )


class OnIssue(RepoScoped):
    issue: IssueRef = Field(
        description="The issue to act on. Give exactly one: its number if the message has one, "
        "else a description."
    )


class AssignIssue(OnIssue):
    assignees: tuple[Person, ...] = Field(min_length=1, max_length=10, description="Who to add.")


class UnassignIssue(OnIssue):
    assignees: tuple[Person, ...] = Field(min_length=1, max_length=10, description="Who to remove.")


class SetStatus(OnIssue):
    status: Line = Field(
        min_length=1,
        max_length=50,
        description='The status as the message says it, e.g. "in progress" or "done".',
    )


class AddLabels(OnIssue):
    labels: tuple[Label, ...] = Field(min_length=1, max_length=10, description="Labels to add.")


class RemoveLabels(OnIssue):
    labels: tuple[Label, ...] = Field(min_length=1, max_length=10, description="Labels to remove.")


class CommentOnIssue(OnIssue):
    body: Body = Field(min_length=1, description="The comment text.")


class CloseIssue(OnIssue):
    reason: CloseReason = Field(
        default=CloseReason.COMPLETED,
        description="completed (done or fixed), not_planned (won't do), duplicate.",
    )


class ReopenIssue(OnIssue):
    pass


class ListIssues(RepoScoped):
    state: IssueStateFilter = Field(default="open", description="Which issues to list.")
    assignee: Person | None = Field(default=None, description="Only issues assigned to them.")
    labels: tuple[Label, ...] = Field(
        default=(), max_length=10, description="Only issues with all of these labels."
    )
    limit: int = Field(default=10, ge=1, le=30, description="How many to show at most.")


class MyIssues(RepoScoped):
    state: IssueStateFilter = Field(default="open", description="Which issues to list.")


class AskClarifyingQuestion(Proposal):
    question: Line = Field(
        min_length=1, max_length=500, description="One short question that resolves the doubt."
    )


class NotARequest(Proposal):
    pass


@dataclass(frozen=True, slots=True)
class ActionSpec:
    name: str
    model: type[Proposal]
    description: str
    permission: Action | None  # None: nothing happens on the code host
    needs_confirmation: bool


_SPECS = (
    ActionSpec(
        "create_issue",
        CreateIssue,
        "Create one issue from a bug report, task or feature request.",
        Action.CREATE_ISSUE,
        True,
    ),
    ActionSpec(
        "create_issues",
        CreateIssues,
        "Create several issues when the message clearly asks for more than one.",
        Action.CREATE_ISSUE,
        True,
    ),
    ActionSpec(
        "assign_issue", AssignIssue, "Assign people to an issue.", Action.TRIAGE_ISSUE, True
    ),
    ActionSpec(
        "unassign_issue",
        UnassignIssue,
        "Remove people from an issue's assignees.",
        Action.TRIAGE_ISSUE,
        True,
    ),
    ActionSpec(
        "set_status",
        SetStatus,
        "Move an issue to another status on the team's board (todo, in progress, review, done).",
        Action.TRIAGE_ISSUE,
        False,
    ),
    ActionSpec("add_labels", AddLabels, "Add labels to an issue.", Action.TRIAGE_ISSUE, False),
    ActionSpec(
        "remove_labels", RemoveLabels, "Remove labels from an issue.", Action.TRIAGE_ISSUE, False
    ),
    ActionSpec(
        "comment_on_issue", CommentOnIssue, "Add a comment to an issue.", Action.COMMENT, True
    ),
    ActionSpec("close_issue", CloseIssue, "Close an issue.", Action.TRIAGE_ISSUE, True),
    ActionSpec("reopen_issue", ReopenIssue, "Reopen a closed issue.", Action.TRIAGE_ISSUE, True),
    ActionSpec(
        "list_issues",
        ListIssues,
        "List issues, optionally by assignee, labels or state.",
        Action.VIEW,
        False,
    ),
    ActionSpec(
        "my_issues", MyIssues, "List the issues assigned to the person asking.", Action.VIEW, False
    ),
    ActionSpec(
        "ask_clarifying_question",
        AskClarifyingQuestion,
        "Ask one question when the request is unclear or matches several people or issues.",
        None,
        False,
    ),
    ActionSpec(
        "not_a_request",
        NotARequest,
        "The message asks for nothing chirpo can do.",
        None,
        False,
    ),
)

ACTIONS: Mapping[str, ActionSpec] = {spec.name: spec for spec in _SPECS}
_BY_MODEL: Mapping[type[Proposal], ActionSpec] = {spec.model: spec for spec in _SPECS}


def spec_of(proposal: Proposal) -> ActionSpec:
    return _BY_MODEL[type(proposal)]


def permission_of(proposal: Proposal) -> Action | None:
    """Usually the spec's; creating issues with assignees or labels also triages them, and the
    App's token would apply them for someone GitHub only lets open plain issues."""
    drafts: tuple[IssueDraft, ...] = ()
    if isinstance(proposal, CreateIssue):
        drafts = (proposal,)
    elif isinstance(proposal, CreateIssues):
        drafts = proposal.issues
    if any(draft.assignees or draft.labels for draft in drafts):
        return Action.TRIAGE_ISSUE
    return spec_of(proposal).permission
