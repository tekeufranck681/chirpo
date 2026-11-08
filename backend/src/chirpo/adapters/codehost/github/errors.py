"""Turn githubkit exceptions into the core's CodeHost errors, and decide which calls to retry."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

from githubkit.exception import (
    GitHubException,
    GraphQLFailed,
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
# chirpo sends only read queries over GraphQL (no mutations), so those POSTs are safe to retry.
GRAPHQL_PATH = "/graphql"
# GitHub explains refusals ("At least 1 approving review is required"); keep it, bounded.
MAX_REASON_LENGTH = 200
# A worker job must not sleep for long; a longer rate-limit wait is raised to the caller instead.
MAX_RATE_LIMIT_WAIT = timedelta(seconds=5)

_BY_STATUS: dict[int, type[CodeHostError]] = {
    403: CodeHostForbiddenError,
    404: CodeHostNotFoundError,
    405: CodeHostConflictError,  # e.g. merging a draft, conflicting or blocked pull request
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
        and (exc.request.method in SAFE_TO_RETRY or exc.request.url.path.endswith(GRAPHQL_PATH))
        and retry_count < MAX_SERVER_ERROR_RETRIES
    ):
        return RetryOption(True, timedelta(seconds=(retry_count + 1) ** 2))
    return RetryOption(False)


def _core_error(exc: GitHubException, what: str) -> CodeHostError:
    if isinstance(exc, RequestError) and isinstance(exc.exc, RequestFailed):
        # Minting the installation token failed (wrapped by the auth flow): the App was uninstalled,
        # suspended, or lost access to the repo. That's an access problem, whatever the status.
        if isinstance(exc.exc, RateLimitExceeded):
            return _core_error(exc.exc, what)
        status = exc.exc.response.status_code
        if status < 500:
            return CodeHostForbiddenError(f"{what}: chirpo's GitHub App lost access ({status})")
        return _core_error(exc.exc, what)
    if isinstance(exc, RateLimitExceeded):
        return CodeHostRateLimitedError(exc.retry_after.total_seconds())
    if isinstance(exc, RequestFailed):
        status = exc.response.status_code
        if status == 401:
            return CodeHostForbiddenError(f"{what}: GitHub rejected chirpo's credentials (401)")
        error = _BY_STATUS.get(status, CodeHostUnavailableError)
        reason = _reason(exc) if error is not CodeHostUnavailableError else ""
        return error(f"{what}: GitHub answered {status}{f': {reason}' if reason else ''}")
    if isinstance(exc, GraphQLFailed):
        return _graphql_error(exc, what)
    return CodeHostUnavailableError(f"{what}: GitHub did not answer")


def _reason(exc: RequestFailed) -> str:
    try:
        message = exc.response.raw_response.json().get("message", "")
    except (ValueError, AttributeError):
        return ""
    return _clean(str(message))


def _clean(text: str) -> str:
    """One line, bounded: the text may echo user input and could end up in a chat message."""
    return " ".join(text.split())[:MAX_REASON_LENGTH]


def _graphql_error(exc: GraphQLFailed, what: str) -> CodeHostError:
    errors = exc.response.errors or []
    types = {error.type for error in errors}
    reason = _clean("; ".join(error.message for error in errors))
    if "NOT_FOUND" in types:
        return CodeHostNotFoundError(f"{what}: {reason}")
    if "FORBIDDEN" in types:
        return CodeHostForbiddenError(f"{what}: {reason}")
    if not types - {None}:
        # Untyped errors are GitHub-side hiccups ("Something went wrong", timeouts): retryable.
        return CodeHostUnavailableError(f"{what}: {reason}")
    return CodeHostError(f"{what}: GitHub rejected the query: {reason}")


@contextmanager
def translate_errors(what: str) -> Iterator[None]:
    """Raise core errors instead of githubkit ones. `what` names the call in the message."""
    try:
        yield
    except GitHubException as exc:
        raise _core_error(exc, what) from exc
