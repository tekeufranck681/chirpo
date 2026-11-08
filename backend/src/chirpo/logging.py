"""Logging (structlog, also applied to stdlib loggers) and optional Sentry.

log = get_logger(__name__)
with bound_context(workspace_id=str(ws.id)):
    log.info("issue_created", issue_number=87)
"""

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars as bind_context
from structlog.contextvars import bound_contextvars as bound_context
from structlog.contextvars import clear_contextvars as clear_context
from structlog.typing import Processor

from chirpo import __version__
from chirpo.config import Settings

__all__ = [
    "bind_context",
    "bound_context",
    "clear_context",
    "configure_logging",
    "get_logger",
    "init_sentry",
]

REDACTED = "[redacted]"
SENSITIVE_WORDS = {"token", "secret", "password", "passwd", "authorization", "cookie", "dsn"}
SENSITIVE_PHRASES = ("api_key", "apikey", "private_key", "encryption_key")
SECRET_PATTERNS = (
    re.compile(r"bot\d+:[\w-]+"),  # Telegram bot token, e.g. in api.telegram.org/bot<token>/...
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),  # user:password@ in URLs
    re.compile(r"(?i)bearer\s+[\w.~+/-]+=*"),
)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return bool(SENSITIVE_WORDS & set(normalized.split("_"))) or any(
        phrase in normalized for phrase in SENSITIVE_PHRASES
    )


def scrub(value: Any) -> Any:
    """Redact sensitive keys and secret-looking text, recursively."""
    if isinstance(value, str):
        for pattern in SECRET_PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value
    if isinstance(value, Mapping):
        return {
            k: REDACTED if isinstance(k, str) and _is_sensitive_key(k) else scrub(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(scrub(item) for item in value)
    return value


def redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return scrub(event_dict)  # type: ignore[no-any-return]


def _scrub_sentry_event(event: Any, _hint: Any) -> Any:
    return scrub(event)


def configure_logging(settings: Settings) -> None:
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_secrets,
    ]
    renderer: list[Processor] = (
        [structlog.processors.format_exc_info, redact_secrets, structlog.processors.JSONRenderer()]
        if settings.json_logs
        else [structlog.dev.ConsoleRenderer()]
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *renderer],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)
    # httpx logs full request URLs at INFO; Telegram puts the bot token in the URL path.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger


def init_sentry(settings: Settings) -> bool:
    if settings.sentry_dsn is None:
        return False

    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=settings.env,
        release=f"chirpo@{__version__}",
        send_default_pii=False,
        include_local_variables=False,  # locals can hold decrypted tokens
        before_send=_scrub_sentry_event,
        before_breadcrumb=_scrub_sentry_event,
        traces_sample_rate=0.0,
    )
    return True
