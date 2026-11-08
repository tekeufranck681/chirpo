"""`.env.example` must list exactly the settings the app reads, so it never goes stale."""

import re
from pathlib import Path

from chirpo.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"
VARIABLE = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _example_variables() -> set[str]:
    return set(VARIABLE.findall(ENV_EXAMPLE.read_text()))


def _settings_variables() -> set[str]:
    prefix = Settings.model_config.get("env_prefix", "")
    return {f"{prefix}{name}".upper() for name in Settings.model_fields}


def test_every_setting_is_documented_in_env_example() -> None:
    missing = _settings_variables() - _example_variables()
    assert not missing, f"add these to .env.example: {sorted(missing)}"


def test_env_example_has_no_unknown_chirpo_variables() -> None:
    unknown = {v for v in _example_variables() if v.startswith("CHIRPO_")} - _settings_variables()
    assert not unknown, f"remove from .env.example or add to Settings: {sorted(unknown)}"
