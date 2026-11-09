"""Workspaces: a team with its chats, repos and members, their accounts, and the audit log."""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from chirpo.core.domain import AuditResult, RepoRef
from chirpo.core.permissions import Role


class Workspace(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    keeps_context: bool = True  # recent messages kept for the LLM (D46); admins turn it off


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
