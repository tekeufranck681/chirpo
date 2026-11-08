"""Shared helpers for GitHub tests: saved API payloads and a mocked api.github.com."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import respx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "github"
API = "https://api.github.com"


def payload(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@contextmanager
def mocked_github() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router
