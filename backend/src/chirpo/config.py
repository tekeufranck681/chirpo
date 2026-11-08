"""Settings from CHIRPO_* environment variables or `.env`. The only place that reads env vars."""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from chirpo.security import parse_keys

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
LogFormat = Literal["auto", "json", "console"]
IntakeMode = Literal["polling", "webhook"]
LLMProviderName = Literal["gemini"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CHIRPO_",
        env_file=(".env", "../.env"),  # repo root or backend/
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        hide_input_in_errors=True,  # startup errors must not print secrets
        extra="ignore",
    )

    env: Environment = "development"
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "auto"
    database_url: str = "postgresql+asyncpg://chirpo:chirpo@localhost:5433/chirpo"
    redis_url: str = "redis://localhost:6380/0"
    encryption_key: SecretStr = Field(description="Comma-separated Fernet keys, newest first")
    public_url: AnyHttpUrl | None = None
    sentry_dsn: SecretStr | None = None

    telegram_bot_token: SecretStr | None = None
    telegram_mode: IntakeMode = "polling"
    telegram_webhook_secret: SecretStr | None = None

    github_app_id: int | None = None
    github_app_private_key_path: Path | None = None
    github_webhook_secret: SecretStr | None = None
    github_events_mode: IntakeMode = "polling"
    github_dev_token: SecretStr | None = None

    llm_provider: LLMProviderName = "gemini"
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None

    @field_validator("database_url")
    @classmethod
    def _async_postgres_driver(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("must start with postgresql+asyncpg:// (the async driver)")
        return value

    @field_validator("encryption_key")
    @classmethod
    def _valid_fernet_keys(cls, value: SecretStr) -> SecretStr:
        parse_keys(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def _webhook_modes_are_complete(self) -> Self:
        if self.telegram_mode == "webhook" and not (
            self.public_url and self.telegram_webhook_secret
        ):
            raise ValueError("telegram_mode=webhook needs public_url and telegram_webhook_secret")
        if self.github_events_mode == "webhook" and not (
            self.public_url and self.github_webhook_secret
        ):
            raise ValueError(
                "github_events_mode=webhook needs public_url and github_webhook_secret"
            )
        if self.env == "production" and self.github_dev_token:
            raise ValueError("github_dev_token is for development only; use the GitHub App")
        return self

    @property
    def encryption_keys(self) -> list[str]:
        return parse_keys(self.encryption_key.get_secret_value())

    @property
    def json_logs(self) -> bool:
        if self.log_format == "auto":
            return self.env != "development"
        return self.log_format == "json"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
