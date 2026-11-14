"""What the LLM is told about the team for one request (D47).

Everything here was written by chat users or comes from the code host, so it is untrusted: each
block is capped in size and later wrapped in a marker-named block by `interpret_request`.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from chirpo.core.actions import hidden_char, unsafe_char
from chirpo.core.domain import AttachmentKind, Issue, RecentMessage, RepoRef
from chirpo.core.images import can_read
from chirpo.core.pending import Step
from chirpo.core.statuses import StatusLabels
from chirpo.core.workspaces import LinkedRepo, Member

MAX_MEMBERS = 50
MAX_ISSUES = 30
MAX_ISSUE_LINE = 300
MAX_MESSAGE_LINE = 500
MAX_NAME = 100
MAX_PROPOSAL_CHARS = 40_000  # a card bigger than this isn't changed by reply: nothing is cut

Block = tuple[str, str]  # (kind, body)


@dataclass(frozen=True, slots=True)
class Team:
    members: Sequence[Member]
    repos: Sequence[LinkedRepo]
    open_issues: Sequence[Issue]  # of the default repo, newest first
    statuses: StatusLabels = field(default_factory=StatusLabels)  # the workspace's (M6.4)

    @property
    def default_repo(self) -> RepoRef:
        return default_of(self.repos)


def default_of(repos: Sequence[LinkedRepo]) -> RepoRef:
    """The default repo (there is always one once a repo is linked; else the first)."""
    return next((link.repo for link in repos if link.is_default), repos[0].repo)


def context_blocks(
    team: Team,
    *,
    recent: Sequence[RecentMessage],
    replied: RecentMessage | None,
    media: bool = False,
    look_at: str | None = None,
    proposal: Sequence[Step] | None = None,
) -> list[Block]:
    """`media`: say which messages came with files; `look_at`: the message whose image chirpo
    can look at if the request refers to it (D52)."""
    blocks: list[Block] = [
        ("team_members", "\n".join(_member(m) for m in team.members[:MAX_MEMBERS])),
        ("linked_repos", "\n".join(_repo(link) for link in team.repos)),
        ("open_issues", "\n".join(_issue(i) for i in team.open_issues[:MAX_ISSUES]) or "none"),
    ]
    if recent:
        said = "\n".join(_said(m, media=media, look_at=look_at) for m in recent)
        blocks.append(("recent_messages", said))
    if replied is not None:
        blocks.append(("replied_message", _said(replied, media=media)))
    if proposal is not None:
        blocks.append(("pending_proposal", "\n".join(_step(step) for step in proposal)))
    return blocks


def _step(step: Step) -> str:
    """A stored step as one line: its tool and arguments, as the model would have called it."""
    return clean(
        f"- {step.action} {json.dumps(step.proposal, ensure_ascii=False)}", MAX_PROPOSAL_CHARS
    )


def proposal_size(steps: Sequence[Step]) -> int:
    """What `pending_proposal` would weigh, before any cap: the model is told to copy it, so a
    proposal that is too big is refused rather than cut."""
    return sum(
        len(f"- {step.action} {json.dumps(step.proposal, ensure_ascii=False)}") for step in steps
    )


def _member(member: Member) -> str:
    handle = f" (@{member.chat_username})" if member.chat_username else ""
    github = f"GitHub @{member.codehost.login}" if member.codehost else "GitHub not linked"
    return f"- {clean(member.display_name, MAX_NAME)}{handle}, {github}"


def _repo(link: LinkedRepo) -> str:
    return f"- {link.repo.full_name}{' (default)' if link.is_default else ''}"


def _issue(issue: Issue) -> str:
    details = []
    if issue.assignees:
        details.append("assigned: " + ", ".join(f"@{login}" for login in issue.assignees))
    if issue.labels:
        details.append("labels: " + ", ".join(issue.labels))
    suffix = f" ({'; '.join(details)})" if details else ""
    return clean(f"#{issue.number} {issue.title}{suffix}", MAX_ISSUE_LINE)


def _said(message: RecentMessage, *, media: bool = False, look_at: str | None = None) -> str:
    name = clean(message.sender.display_name, MAX_NAME)
    shown = " ".join(_media(message, look_at)) if media else ""
    body = f"{shown} {message.text}".strip() if shown else message.text
    return clean(f"[{message.sent_at:%H:%M}] {name}: {body}", MAX_MESSAGE_LINE).rstrip()


def _media(message: RecentMessage, look_at: str | None) -> list[str]:
    """One marker per file, written by us from what the platform says, never from the text."""
    markers, lookable = [], message.message_id == look_at
    for attachment in message.attachments:
        if attachment.kind is AttachmentKind.IMAGE:
            if lookable and can_read(attachment):
                markers.append("[image, can be looked at]")
                lookable = False  # the first readable one is the one chirpo would fetch
            else:
                markers.append("[image]")
        elif attachment.kind is AttachmentKind.VOICE:
            markers.append("[voice message]")
        else:
            markers.append("[file]")
    return markers


def clean(text: str, limit: int) -> str:
    """One visible line: no control, bidi, zero-width or format characters, no line breaks."""
    visible = "".join(c for c in text if not unsafe_char(c) and not hidden_char(c))
    return " ".join(visible.split())[:limit]
