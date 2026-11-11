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


class AttachmentError(MessagingError):
    """A file sent in the chat can't be fetched or used."""


class AttachmentTooLargeError(AttachmentError):
    pass


class AttachmentUnavailableError(AttachmentError):
    """The platform no longer has the file, or never had it."""


class UnsupportedImageError(AttachmentError):
    """Not a JPEG, PNG or WebP, whatever the sender or the platform called it."""


class CountersUnavailableError(Exception):
    """The store behind the rate limits can't be reached."""


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


class InvalidProposalError(ValueError):
    """The LLM proposed an unknown action or arguments that don't validate.

    `problems` name the fields and error types only, never the values, which are untrusted text.
    """

    def __init__(self, action: str, problems: tuple[str, ...]) -> None:
        super().__init__(f"invalid {action!r} proposal: {'; '.join(problems)}")
        self.action = action
        self.problems = problems


class LLMError(Exception):
    """An LLM call failed."""


class LLMSetupError(LLMError):
    """The key, the model or the request is wrong; retrying won't help until the setup is fixed.

    `detail` is the provider's own explanation, kept out of the message: it may quote the request.
    """

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class LLMRateLimitedError(LLMError):
    def __init__(self, retry_after_seconds: float | None) -> None:
        super().__init__(
            f"rate limited, retry after {retry_after_seconds} s"
            if retry_after_seconds is not None
            else "rate limited"
        )
        self.retry_after_seconds = retry_after_seconds


class LLMUnavailableError(LLMError):
    """Network problem or provider outage; retrying later may succeed."""


class LLMRefusedError(LLMError):
    """The provider's safety filters blocked the message or the answer."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"blocked by the provider: {reason}")
        self.reason = reason


class LLMInvalidOutputError(LLMError):
    """The answer holds no usable proposal. `problems` never include the model's text."""

    def __init__(self, problems: tuple[str, ...]) -> None:
        super().__init__(f"unusable answer: {'; '.join(problems)}")
        self.problems = problems
