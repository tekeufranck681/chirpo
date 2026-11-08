"""Turn githubkit exceptions into the core's CodeHost errors, and decide which calls to retry."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

from githubkit.exception import (
    GitHubException,
    RateLimitExceeded,
    RequestError,
    RequestFailed,
)
from githubkit.retry import RetryOption

from chirpo.core.errors import (
    CodeHostConflictError,
    CodeHostError,
    CodeHostForbiddenError,
    CodeHostNotFoundError,
    CodeHostRateLimitedError,
    CodeHostUnavailableError,
    CodeHostValidationError,
)

MAX_SERVER_ERROR_RETRIES = 2
# A write that failed with 5xx may still have happened; retrying it could duplicate an issue.
SAFE_TO_RETRY = frozenset({"GET", "HEAD"})
# A worker job must not sleep for long; a longer rate-limit wait is raised to the caller instead.
MAX_RATE_LIMIT_WAIT = timedelta(seconds=5)

_BY_STATUS: dict[int, type[CodeHostError]] = {
    403: CodeHostForbiddenError,
    404: CodeHostNotFoundError,
    409: CodeHostConflictError,
    422: CodeHostValidationError,
}


def retry_policy(exc: GitHubException, retry_count: int) -> RetryOption:
    if isinstance(exc, RateLimitExceeded):
        short_wait = exc.retry_after <= MAX_RATE_LIMIT_WAIT
        return RetryOption(retry_count == 0 and short_wait, exc.retry_after)
    if (
        isinstance(exc, RequestFailed)
        and exc.response.status_code >= 500
        and exc.request.method in SAFE_TO_RETRY
        and retry_count < MAX_SERVER_ERROR_RETRIES
    ):
        return RetryOption(True, timedelta(seconds=(retry_count + 1) ** 2))
    return RetryOption(False)


def _core_error(exc: GitHubException, what: str) -> CodeHostError:
    if isinstance(exc, RequestError) and isinstance(exc.exc, GitHubException):
        # Failures inside the auth flow (e.g. minting a token) arrive wrapped.
        return _core_error(exc.exc, what)
    if isinstance(exc, RateLimitExceeded):
        return CodeHostRateLimitedError(exc.retry_after.total_seconds())
    if isinstance(exc, RequestFailed):
        status = exc.response.status_code
        if status == 401:
            return CodeHostForbiddenError(f"{what}: GitHub rejected chirpo's credentials (401)")
        return _BY_STATUS.get(status, CodeHostUnavailableError)(f"{what}: GitHub answered {status}")
    return CodeHostUnavailableError(f"{what}: GitHub did not answer")


@contextmanager
def translate_errors(what: str) -> Iterator[None]:
    """Raise core errors instead of githubkit ones. `what` names the call in the message."""
    try:
        yield
    except GitHubException as exc:
        raise _core_error(exc, what) from exc
