"""A proposal waiting for a person to confirm it (M5, D53).

What is stored is the validated, resolved proposals, exactly as the card showed them: pressing
Confirm runs this stored payload, never anything the button or the chat says.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from chirpo.core.actions import ACTIONS, Proposal, spec_of
from chirpo.core.domain import ChatRef, RepoRef


class PendingStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"
    REJECTED = "rejected"
    MODIFIED = "modified"
    EXPIRED = "expired"


class Step(BaseModel):
    """One resolved proposal, in a form that goes into the database and comes back identical."""

    model_config = ConfigDict(frozen=True)

    action: str
    provider: str
    proposal: dict[str, Any]

    @classmethod
    def of(cls, proposal: Proposal, repo: RepoRef) -> "Step":
        return cls(
            action=spec_of(proposal).name,
            provider=repo.provider,
            proposal=proposal.model_dump(mode="json"),
        )

    def load(self) -> tuple[RepoRef, Proposal]:
        """Validated again: a stored row is not trusted more than a model's answer."""
        proposal = ACTIONS[self.action].model.model_validate(self.proposal)
        repo = getattr(proposal, "repo", None)
        if not isinstance(repo, str):
            raise ValueError("a stored step names no repo")
        return RepoRef.parse(self.provider, repo), proposal


class PendingAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    workspace_id: UUID
    requested_by: UUID  # the member who asked: their permissions are the ones that count
    chat: ChatRef
    message_id: str | None = None  # the card; None until it was sent
    steps: tuple[Step, ...] = Field(min_length=1)
    status: PendingStatus = PendingStatus.PROPOSED
    confirmed_by: UUID | None = None
    expires_at: AwareDatetime
    result: dict[str, Any] = Field(default_factory=dict)

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at <= now
