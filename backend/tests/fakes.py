"""Fake adapters for testing the core without network calls."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from chirpo.core.domain import (
    ChatRef,
    ChatUser,
    InboundCallback,
    InboundEvent,
    InboundMessage,
    SentMessage,
)

CHAT = ChatRef(platform="fake", chat_id="-100")
USER = ChatUser(platform="fake", user_id="42", display_name="Paul", username="paul")


def make_message(text: str, **overrides: Any) -> InboundMessage:
    values: dict[str, Any] = {
        "event_id": "1",
        "chat": CHAT,
        "sender": USER,
        "message_id": "10",
        "text": text,
        "sent_at": datetime(2026, 1, 1, tzinfo=UTC),
        **overrides,
    }
    return InboundMessage(**values)


def make_callback(data: str) -> InboundCallback:
    return InboundCallback(event_id="2", chat=CHAT, sender=USER, message_id="11", data=data)


class FakeMessaging:
    platform = "fake"

    def __init__(self) -> None:
        self.sent: list[tuple[ChatRef, str, str | None]] = []
        self.edited: list[tuple[SentMessage, str]] = []

    def parse_update(self, raw: Mapping[str, Any]) -> InboundEvent | None:
        return None

    async def send_message(
        self, chat: ChatRef, text: str, *, reply_to: str | None = None
    ) -> SentMessage:
        self.sent.append((chat, text, reply_to))
        return SentMessage(chat=chat, message_id=str(100 + len(self.sent)))

    async def edit_message(self, message: SentMessage, text: str) -> None:
        self.edited.append((message, text))
