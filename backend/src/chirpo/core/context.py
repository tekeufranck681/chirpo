"""What the LLM is told about the team for one request (D47).

Everything here was written by chat users or comes from the code host, so it is untrusted: each
block is capped in size and later wrapped in a marker-named block by `interpret_request`.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from chirpo.core.actions import unsafe_char
from chirpo.core.domain import Issue, RecentMessage, RepoRef
from chirpo.core.workspaces import LinkedRepo, Member

MAX_MEMBERS = 50
MAX_ISSUES = 30
MAX_ISSUE_LINE = 300
MAX_MESSAGE_LINE = 500
MAX_NAME = 100

Block = tuple[str, str]  # (kind, body)


@dataclass(frozen=True, slots=True)
class Team:
    members: Sequence[Member]
    repos: Sequence[LinkedRepo]
    open_issues: Sequence[Issue]  # of the default repo, newest first

    @property
    def default_repo(self) -> RepoRef:
        return default_of(self.repos)


def default_of(repos: Sequence[LinkedRepo]) -> RepoRef:
    """The default repo (there is always one once a repo is linked; else the first)."""
    return next((link.repo for link in repos if link.is_default), repos[0].repo)


def context_blocks(
    team: Team, *, recent: Sequence[RecentMessage], replied: RecentMessage | None
) -> list[Block]:
    blocks: list[Block] = [
        ("team_members", "\n".join(_member(m) for m in team.members[:MAX_MEMBERS])),
        ("linked_repos", "\n".join(_repo(link) for link in team.repos)),
        ("open_issues", "\n".join(_issue(i) for i in team.open_issues[:MAX_ISSUES]) or "none"),
    ]
    if recent:
        blocks.append(("recent_messages", "\n".join(_said(m) for m in recent)))
    if replied is not None:
        blocks.append(("replied_message", _said(replied)))
    return blocks


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


def _said(message: RecentMessage) -> str:
    name = clean(message.sender.display_name, MAX_NAME)
    return clean(f"[{message.sent_at:%H:%M}] {name}: {message.text}", MAX_MESSAGE_LINE)


def clean(text: str, limit: int) -> str:
    """One visible line: no control, bidi or zero-width characters, no line breaks."""
    visible = "".join(char for char in text if not unsafe_char(char))
    return " ".join(visible.split())[:limit]
