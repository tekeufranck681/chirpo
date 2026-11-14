"""The four statuses chirpo sets, and the GitHub label each one is in a workspace (M6.2, M6.4).

An issue has at most one of the four labels. Teams that already use their own labels ("WIP",
"Needs review") name them with /settings; any other label is never touched by a status change.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from chirpo.core import messages
from chirpo.core.actions import hidden_char, unsafe_char

Status = Literal["todo", "in progress", "review", "done"]
STATUSES: tuple[Status, ...] = ("todo", "in progress", "review", "done")
DEFAULT_PREFIX = "status: "
_WORD_PREFIX = DEFAULT_PREFIX.strip()
# GitHub's labels for a new repo: making one a status would strip it from every issue moved.
GITHUB_LABELS = frozenset(
    {"bug", "documentation", "duplicate", "enhancement", "good first issue", "help wanted",
     "invalid", "question", "wontfix"}
)  # fmt: skip
MAX_LABEL = 50  # GitHub's limit for a label name
COLORS: dict[Status, str] = {
    "todo": "ededed",
    "in progress": "fbca04",
    "review": "5319e7",
    "done": "0e8a16",
}
_WORDS: dict[str, Status] = {
    "to do": "todo",
    "backlog": "todo",
    "doing": "in progress",
    "wip": "in progress",
    "started": "in progress",
    "working": "in progress",
    "in review": "review",
    "needs review": "review",
    "reviewing": "review",
    "finished": "done",
    "complete": "done",
    "completed": "done",
}


class StatusLabels(BaseModel):
    """The label for each status; the defaults are `status: todo` and so on."""

    model_config = ConfigDict(frozen=True)

    todo: str = "status: todo"
    in_progress: str = "status: in progress"
    review: str = "status: review"
    done: str = "status: done"

    def label(self, status: Status) -> str:
        return str(getattr(self, _field(status)))

    def status_of(self, label: str) -> Status | None:
        wanted = label.strip().casefold()
        return next((s for s in STATUSES if self.label(s).casefold() == wanted), None)

    def is_status(self, label: str) -> bool:
        return self.status_of(label) is not None

    def with_label(self, status: Status, label: str) -> "StatusLabels":
        return self.model_copy(update={_field(status): label})


def _field(status: Status) -> str:
    return status.replace(" ", "_")


def status_named(said: str, labels: StatusLabels) -> Status | None:
    """The status a person or the model named: one of the workspace's label names, or a status
    word ("doing", "in-progress", "status: done")."""
    if found := labels.status_of(said):
        return found
    return _word_status(said)


def _word_status(said: str) -> Status | None:
    words = said.casefold().removeprefix(_WORD_PREFIX)
    key = " ".join(words.replace("-", " ").replace("_", " ").split())
    key = _WORDS.get(key, key)
    return next((status for status in STATUSES if status == key), None)


def shown_status(label: str) -> str:
    """How a status label reads in chat: `status: review` as "review", a team's own as it is."""
    return label.removeprefix(DEFAULT_PREFIX)


def label_problem(name: str, labels: StatusLabels, status: Status) -> str | None:
    """Why `name` can't be the label for `status`, said for people; None if it can."""
    if not name or len(name) > MAX_LABEL:
        return messages.STATUS_LABEL_LENGTH.format(limit=MAX_LABEL)
    if name in (".", "..") or any(c in "\t\n" or unsafe_char(c) or hidden_char(c) for c in name):
        return messages.STATUS_LABEL_CHARACTERS
    if name.casefold() in GITHUB_LABELS:
        return messages.STATUS_LABEL_ORDINARY.format(label=name.casefold())
    # Another status's label, or a word that means another status ("done" for todo), would make
    # "move #5 to done" mean something else.
    taken = labels.status_of(name) or _word_status(name)
    if taken is not None and taken != status:
        return messages.STATUS_LABEL_TAKEN.format(status=taken)
    return None


def clears(labels: StatusLabels) -> list[str]:
    """The labels a status change takes off: the workspace's four, and chirpo's defaults, which
    issues keep from before a rename (otherwise they'd end up with two statuses)."""
    names = [labels.label(s) for s in STATUSES]
    names += [d for d in (StatusLabels().label(s) for s in STATUSES) if d not in names]
    return names
