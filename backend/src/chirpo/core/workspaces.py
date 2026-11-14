"""Workspaces: a team with its chats, repos and members, their accounts, and the audit log."""

from datetime import date, time
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from chirpo.core.domain import AuditResult, ChatRef, RepoRef
from chirpo.core.permissions import Role
from chirpo.core.statuses import StatusLabels

# How chatty the bot is when someone makes small talk (D24, M6.11).
Personality = Literal["off", "light", "playful"]
PERSONALITIES: tuple[Personality, ...] = ("off", "light", "playful")
DEFAULT_PERSONALITY: Personality = "light"


class Workspace(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    keeps_context: bool = True  # recent messages kept for the LLM (D46); admins turn it off
    personality: Personality = DEFAULT_PERSONALITY  # admins change it with /personality
    statuses: StatusLabels = StatusLabels()  # the team's status labels (/settings status)
    issue_language: str | None = None  # a code from core/languages.py; None: the message's own
    timezone: str = "UTC"  # an IANA name checked by core/summary.timezone_named (M6.5)


class SummarySchedule(BaseModel):
    """When a chat gets its daily summary (M6.5), in the workspace's timezone."""

    model_config = ConfigDict(frozen=True)

    at: time
    weekdays_only: bool = False


class ScheduledSummary(BaseModel):
    """A chat that has a daily summary, with what deciding and writing it needs."""

    model_config = ConfigDict(frozen=True)

    chat: ChatRef
    workspace: Workspace
    schedule: SummarySchedule
    sent_on: date | None = None  # the team's local day it was last posted


class LinkedRepo(BaseModel):
    model_config = ConfigDict(frozen=True)

    repo: RepoRef
    is_default: bool
    installation_id: int | None = None


class CodeHostAccount(BaseModel):
    """A code host account proven to be the member's (device login), never one typed in chat."""

    model_config = ConfigDict(frozen=True)

    provider: str
    user_id: str  # stable across renames, unlike the login
    login: str


class Member(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    workspace_id: UUID
    display_name: str
    role: Role
    chat_username: str | None = None
    codehost: CodeHostAccount | None = None

    @property
    def is_verified(self) -> bool:
        return self.codehost is not None


class AuditEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: UUID | None
    actor_member_id: UUID | None
    action: str
    result: AuditResult
    payload: dict[str, Any] = Field(default_factory=dict)
    confirmed_by_member_id: UUID | None = None
    error: str | None = None
