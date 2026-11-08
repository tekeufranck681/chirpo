"""Domain models (pure Pydantic, no I/O): InboundMessage, Workspace, Member, Issue, ...

Models arrive with the features that need them; for now only the enums the database relies on.
"""

from enum import StrEnum


class IdentityKind(StrEnum):
    """Which kind of account an identity is: a chat account or a code host account."""

    CHAT = "chat"
    CODEHOST = "codehost"


class AuditResult(StrEnum):
    """Outcome of an action, as recorded in the audit log."""

    EXECUTED = "executed"
    FAILED = "failed"
    DENIED = "denied"
    REJECTED = "rejected"
