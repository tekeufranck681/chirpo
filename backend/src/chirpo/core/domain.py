"""Domain models (pure Pydantic, no I/O): InboundMessage, Workspace, Member, Issue, ...

Models arrive with the features that need them.
"""

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict


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


class ChatRef(BaseModel):
    """Where a message lives. `thread_id` is a topic inside a group (Telegram forum topics)."""

    model_config = ConfigDict(frozen=True)

    platform: str
    chat_id: str
    thread_id: str | None = None


class ChatUser(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: str
    user_id: str
    display_name: str
    username: str | None = None
    is_bot: bool = False
    # Posted "as the chat" (e.g. anonymous group admin): no individual identity, so no privileges.
    is_anonymous: bool = False


class InboundMessage(BaseModel):
    """A chat message, normalised by a messaging adapter. `event_id` is unique per platform.

    `text` has mentions of the bot removed (then `mentions_bot` is True); a media caption counts as
    text, and media without a caption has empty text.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str
    chat: ChatRef
    sender: ChatUser
    message_id: str
    text: str
    sent_at: AwareDatetime
    mentions_bot: bool = False
    replies_to_bot: bool = False
    is_private_chat: bool = False


class InboundCallback(BaseModel):
    """A button press on one of the bot's messages."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    chat: ChatRef
    sender: ChatUser
    message_id: str
    data: str


InboundEvent = InboundMessage | InboundCallback


class SentMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    chat: ChatRef
    message_id: str
