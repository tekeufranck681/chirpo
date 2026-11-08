"""Typed errors raised by adapters, so the core never sees platform exceptions."""


class MessagingError(Exception):
    """A messaging platform call failed."""


class MessagingForbiddenError(MessagingError):
    """The bot may not act here: removed from the chat, blocked, or missing rights."""


class MessagingRateLimitedError(MessagingError):
    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after_seconds} s")
        self.retry_after_seconds = retry_after_seconds


class MessagingTimeoutError(MessagingError):
    """No answer in time. The message may still have been delivered: retrying can duplicate it."""


class MessagingUnavailableError(MessagingError):
    """Network problem or platform outage; retrying later may succeed."""
