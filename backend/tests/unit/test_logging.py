import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from chirpo import logging as chirpo_logging
from chirpo.config import Settings
from chirpo.logging import REDACTED, bound_context, configure_logging, get_logger, init_sentry
from chirpo.security import generate_key


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, encryption_key=generate_key(), **overrides)


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    logging.getLogger().handlers = []


def _json_lines(output: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def test_json_output_includes_level_time_and_bound_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(make_settings(log_format="json"))
    with bound_context(workspace_id="ws-1", job_id="job-9"):
        get_logger("test").info("issue_created", issue_number=87)

    (line,) = _json_lines(capsys.readouterr().out)
    assert line["event"] == "issue_created"
    assert line["issue_number"] == 87
    assert line["workspace_id"] == "ws-1"
    assert line["job_id"] == "job-9"
    assert line["level"] == "info"
    assert line["timestamp"].endswith("Z")


def test_context_does_not_leak_after_the_block(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(make_settings(log_format="json"))
    with bound_context(workspace_id="ws-1"):
        pass
    get_logger().info("later")
    (line,) = _json_lines(capsys.readouterr().out)
    assert "workspace_id" not in line


@pytest.mark.parametrize(
    "field", ["token", "bot_token", "api_key", "password", "Authorization", "github_webhook_secret"]
)
def test_secret_looking_fields_are_redacted(field: str, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(make_settings(log_format="json"))
    get_logger().info("calling_api", **{field: "s3cret-value"})
    output = capsys.readouterr().out
    assert "s3cret-value" not in output
    assert _json_lines(output)[0][field] == REDACTED


def test_standard_library_logs_use_the_same_json_format(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(make_settings(log_format="json"))
    logging.getLogger("sqlalchemy.engine").warning("slow query")
    (line,) = _json_lines(capsys.readouterr().out)
    assert line["event"] == "slow query"
    assert line["logger"] == "sqlalchemy.engine"
    assert line["level"] == "warning"


def test_log_level_filters_lower_levels(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(make_settings(log_format="json", log_level="WARNING"))
    get_logger().info("hidden")
    get_logger().warning("shown")
    assert [line["event"] for line in _json_lines(capsys.readouterr().out)] == ["shown"]


def test_sentry_stays_off_without_dsn() -> None:
    assert init_sentry(make_settings()) is False


def test_sentry_starts_without_personal_data(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("sentry_sdk.init", lambda **kwargs: calls.append(kwargs))
    started = init_sentry(make_settings(sentry_dsn="https://key@sentry.example.com/1"))
    assert started is True
    options = calls[0]
    assert options["send_default_pii"] is False
    assert options["include_local_variables"] is False
    assert options["environment"] == "development"
    scrubbed = options["before_send"]({"extra": {"bot_token": "123:abc"}}, None)
    assert scrubbed["extra"]["bot_token"] == REDACTED


def test_module_name_does_not_shadow_standard_logging() -> None:
    # chirpo.logging is our module; `import logging` inside it must still be the stdlib one.
    assert chirpo_logging.logging is logging


def test_nested_secrets_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(make_settings(log_format="json"))
    get_logger().info("call", headers={"Authorization": "Bearer abc"}, items=[{"api_key": "k"}])
    (line,) = _json_lines(capsys.readouterr().out)
    assert line["headers"]["Authorization"] == REDACTED
    assert line["items"][0]["api_key"] == REDACTED


@pytest.mark.parametrize(
    ("text", "leaked"),
    [
        ("GET https://api.telegram.org/bot123456:ABC-def_ghi/getUpdates", "ABC-def_ghi"),
        ("connect postgresql+asyncpg://chirpo:HUNTER2@db/chirpo failed", "HUNTER2"),
        ("header Bearer eyJhbGciOi.payload.sig", "eyJhbGciOi"),
    ],
)
def test_secret_looking_text_is_scrubbed(
    text: str, leaked: str, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(make_settings(log_format="json"))
    get_logger().info(text)
    assert leaked not in capsys.readouterr().out


def test_secrets_in_exception_tracebacks_are_scrubbed(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(make_settings(log_format="json"))
    try:
        raise ConnectionError("https://api.telegram.org/bot123:SECRET-token/sendMessage")
    except ConnectionError:
        get_logger().exception("send_failed")
    assert "SECRET-token" not in capsys.readouterr().out


def test_counts_with_token_in_the_name_are_not_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(make_settings(log_format="json"))
    get_logger().info("llm_call", tokens_used=812)
    (line,) = _json_lines(capsys.readouterr().out)
    assert line["tokens_used"] == 812


def test_http_client_request_logs_are_silenced() -> None:
    configure_logging(make_settings(log_format="json", log_level="DEBUG"))
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING


def test_console_tracebacks_are_scrubbed(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(make_settings(log_format="console"))
    try:
        raise ConnectionError("postgresql+asyncpg://chirpo:HUNTER2@db/chirpo refused")
    except ConnectionError:
        get_logger().exception("db_failed")
    assert "HUNTER2" not in capsys.readouterr().out
