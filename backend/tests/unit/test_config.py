from typing import Any

import pytest
from pydantic import ValidationError

from chirpo.config import Settings
from chirpo.security import generate_key


def make_settings(**overrides: Any) -> Settings:
    """Settings built only from the given values: no .env file, no CHIRPO_ variables."""
    values: dict[str, Any] = {"encryption_key": generate_key(), **overrides}
    return Settings(_env_file=None, **values)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in list(os.environ):
        if name.startswith("CHIRPO_"):
            monkeypatch.delenv(name)


def test_defaults_work_for_local_development() -> None:
    settings = make_settings()
    assert settings.env == "development"
    assert settings.telegram_mode == "polling"
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.json_logs is False


def test_reads_prefixed_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHIRPO_ENCRYPTION_KEY", generate_key())
    monkeypatch.setenv("CHIRPO_LOG_LEVEL", "DEBUG")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.log_level == "DEBUG"


def test_empty_variables_mean_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    # A copied .env.example has lines like `CHIRPO_PUBLIC_URL=`; they must not fail validation.
    monkeypatch.setenv("CHIRPO_ENCRYPTION_KEY", generate_key())
    monkeypatch.setenv("CHIRPO_PUBLIC_URL", "")
    monkeypatch.setenv("CHIRPO_GITHUB_APP_ID", "")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.public_url is None
    assert settings.github_app_id is None


def test_encryption_key_is_required() -> None:
    with pytest.raises(ValidationError, match="encryption_key"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_invalid_encryption_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not a valid Fernet key"):
        make_settings(encryption_key="not-a-key")


def test_several_keys_for_rotation() -> None:
    new, old = generate_key(), generate_key()
    assert make_settings(encryption_key=f"{new},{old}").encryption_keys == [new, old]


def test_sync_database_driver_is_rejected() -> None:
    with pytest.raises(ValidationError, match="asyncpg"):
        make_settings(database_url="postgresql://u:p@localhost/db")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"telegram_mode": "webhook"}, "telegram_mode=webhook"),
        ({"github_events_mode": "webhook"}, "github_events_mode=webhook"),
        ({"env": "production", "github_dev_token": "ghp_x"}, "development only"),
        ({"github_app_id": 123}, "github_app_private_key_path"),
    ],
)
def test_incomplete_or_unsafe_combinations_are_rejected(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(**overrides)


def test_webhook_mode_with_url_and_secret_is_valid() -> None:
    settings = make_settings(
        telegram_mode="webhook",
        public_url="https://chirpo.example.com",
        telegram_webhook_secret="s3cret",
    )
    assert settings.telegram_mode == "webhook"


def test_secrets_never_appear_in_repr_or_str() -> None:
    settings = make_settings(telegram_bot_token="123:ABC-token", llm_api_key="sk-key")
    for text in (repr(settings), str(settings), str(settings.model_dump())):
        assert "123:ABC-token" not in text
        assert "sk-key" not in text


@pytest.mark.parametrize(
    ("env", "log_format", "expected"),
    [
        ("development", "auto", False),
        ("production", "auto", True),
        ("development", "json", True),
        ("production", "console", False),
    ],
)
def test_json_logs_choice(env: str, log_format: str, expected: bool) -> None:
    assert make_settings(env=env, log_format=log_format).json_logs is expected


def test_validation_errors_do_not_echo_secret_input() -> None:
    with pytest.raises(ValidationError) as error:
        make_settings(database_url="postgresql://user:HUNTER2@db/chirpo")
    assert "HUNTER2" not in str(error.value)


def test_message_buffer_keeps_a_day_by_default_and_can_be_off() -> None:
    assert make_settings().message_buffer_hours == 24
    assert make_settings(message_buffer_hours=0).message_buffer_hours == 0
    for hours in (-1, 24 * 31):
        with pytest.raises(ValidationError):
            make_settings(message_buffer_hours=hours)


def test_llm_thinking_level() -> None:
    assert make_settings().llm_thinking is None
    assert make_settings(llm_thinking="minimal").llm_thinking == "minimal"
    with pytest.raises(ValidationError):
        make_settings(llm_thinking="lots")


def test_rate_limit_defaults_and_bounds() -> None:
    settings = make_settings()
    assert settings.llm_calls_per_member_hour == 30
    assert settings.llm_calls_per_workspace_hour == 120
    assert settings.llm_tokens_per_day == 0  # off: the provider's own quota stops first
    assert settings.github_writes_per_member_hour == 30
    assert make_settings(llm_calls_per_member_hour=0).llm_calls_per_member_hour == 0
    for name in (
        "llm_calls_per_member_hour",
        "llm_calls_per_workspace_hour",
        "llm_tokens_per_day",
        "github_writes_per_member_hour",
    ):
        with pytest.raises(ValidationError):
            make_settings(**{name: -1})


def test_reading_images_is_on_unless_switched_off() -> None:
    assert make_settings().read_images is True
    assert make_settings(read_images=False).read_images is False


def test_a_proposal_can_be_confirmed_for_an_hour_by_default() -> None:
    assert make_settings().proposal_ttl_minutes == 60
    assert make_settings(proposal_ttl_minutes=15).proposal_ttl_minutes == 15
    for bad in (0, -5, 60 * 24 * 7 + 1):  # it must expire, and not outlive a week
        with pytest.raises(ValidationError):
            make_settings(proposal_ttl_minutes=bad)


def test_a_message_is_stale_after_five_minutes_by_default() -> None:
    assert make_settings().stale_after_minutes == 5
    assert make_settings(stale_after_minutes=1).stale_after_minutes == 1
    for bad in (0, -1, 24 * 60 + 1):
        with pytest.raises(ValidationError):
            make_settings(stale_after_minutes=bad)
