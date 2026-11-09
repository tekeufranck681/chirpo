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


class CodeHostError(Exception):
    """A code host call failed."""


class CodeHostNotFoundError(CodeHostError):
    """The repo, issue, PR or user doesn't exist, or the app can't see it."""


class CodeHostForbiddenError(CodeHostError):
    """The app lacks the permission, or the repo isn't in its installation."""


class CodeHostValidationError(CodeHostError):
    """The code host rejected the input, e.g. an unknown assignee or label."""


class CodeHostConflictError(CodeHostError):
    """The action conflicts with the current state, e.g. merging a closed or draft PR."""


class CodeHostRateLimitedError(CodeHostError):
    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after_seconds} s")
        self.retry_after_seconds = retry_after_seconds


class CodeHostUnavailableError(CodeHostError):
    """Network problem or code host outage; retrying later may succeed."""


class LastAdminError(Exception):
    """The change would leave a workspace without an admin."""
