"""Protocols implemented outside the core: messaging, code host, LLM, repositories."""

from collections.abc import Mapping
from typing import Any, Protocol

from chirpo.core.domain import ChatRef, InboundEvent, SentMessage


class MessagingAdapter(Protocol):
    platform: str

    def parse_update(self, raw: Mapping[str, Any]) -> InboundEvent | None:
        """Normalise a platform payload. None means "not something chirpo handles"."""
        ...

    async def send_message(
        self, chat: ChatRef, text: str, *, reply_to: str | None = None
    ) -> SentMessage: ...

    async def edit_message(self, message: SentMessage, text: str) -> None: ...
