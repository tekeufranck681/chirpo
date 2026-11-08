"""Protocols implemented outside the core: messaging, code host, LLM, repositories."""

from collections.abc import Mapping
from typing import Any, Protocol

from chirpo.core.domain import ChatRef, InboundEvent, SentMessage


class MessagingAdapter(Protocol):
    platform: str
    # Names the stream event ids come from (platform + bot account), e.g. "telegram:7000000001".
    event_source: str

    def parse_update(self, raw: Mapping[str, Any]) -> InboundEvent | None:
        """Normalise a platform payload. None means "not something chirpo handles"."""
        ...

    async def send_message(
        self, chat: ChatRef, text: str, *, reply_to: str | None = None
    ) -> SentMessage: ...

    async def edit_message(self, message: SentMessage, text: str) -> None: ...

    async def aclose(self) -> None: ...


class ProcessedEvents(Protocol):
    async def claim(self, source: str, external_id: str) -> bool:
        """Record an event as processed. False if it already was (a duplicate delivery)."""
        ...


class PollCursors(Protocol):
    async def get(self, source: str, scope: str) -> str | None: ...

    async def set(self, source: str, scope: str, cursor: str) -> None: ...
