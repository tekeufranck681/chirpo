"""Smoke tests for the package and a guard for the core architecture rule."""

import ast
from pathlib import Path

import chirpo

CORE_DIR = Path(chirpo.__file__).parent / "core"

# Rule 1 in CLAUDE.md: core/ works only with our own domain types.
# It must never import platform SDKs, I/O libraries, or the outer layers of chirpo.
FORBIDDEN_IN_CORE = {
    "telegram",
    "githubkit",
    "google",
    "httpx",
    "sqlalchemy",
    "asyncpg",
    "redis",
    "arq",
    "fastapi",
    "chirpo.adapters",
    "chirpo.db",
    "chirpo.api",
    "chirpo.workers",
}


def test_version_is_exposed() -> None:
    assert chirpo.__version__


def _imported_modules(source: str) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _is_forbidden(module: str) -> bool:
    return any(module == f or module.startswith(f + ".") for f in FORBIDDEN_IN_CORE)


def test_core_imports_no_platform_code() -> None:
    violations = [
        f"{path.relative_to(CORE_DIR)} imports {module}"
        for path in sorted(CORE_DIR.rglob("*.py"))
        for module in _imported_modules(path.read_text())
        if _is_forbidden(module)
    ]
    assert not violations, "core/ must stay platform-agnostic:\n" + "\n".join(violations)


def test_forbidden_check_catches_violations() -> None:
    # Guards the guard: make sure the detection itself works.
    assert _is_forbidden("telegram.ext")
    assert _is_forbidden("chirpo.adapters.codehost.github")
    assert not _is_forbidden("chirpo.core.domain")
    assert not _is_forbidden("pydantic")
