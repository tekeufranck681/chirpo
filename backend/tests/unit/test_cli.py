import os

import pytest

from chirpo.cli import ALEMBIC_INI, build_parser, run_api, run_migrate, run_poll, run_worker


def test_api_defaults_to_localhost() -> None:
    args = build_parser().parse_args(["api"])
    assert (args.host, args.port, args.reload) == ("127.0.0.1", 8000, False)
    assert args.handler is run_api


def test_api_options() -> None:
    args = build_parser().parse_args(["api", "--host", "10.0.0.5", "--port", "9000", "--reload"])
    assert (args.host, args.port, args.reload) == ("10.0.0.5", 9000, True)


@pytest.mark.parametrize(
    ("command", "handler"), [("worker", run_worker), ("poll", run_poll), ("migrate", run_migrate)]
)
def test_subcommands_map_to_handlers(command: str, handler: object) -> None:
    assert build_parser().parse_args([command]).handler is handler


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_migrate_finds_alembic_ini() -> None:
    assert ALEMBIC_INI.is_file()


@pytest.mark.parametrize("action", ["set-webhook", "delete-webhook", "webhook-info"])
def test_telegram_webhook_commands(action: str) -> None:
    from chirpo.cli import run_telegram

    args = build_parser().parse_args(["telegram", action])
    assert (args.handler, args.telegram_command) == (run_telegram, action)


def test_unknown_telegram_command_is_rejected() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["telegram", "nope"])


@pytest.mark.parametrize(("argv", "repo"), [([], None), (["octo/app"], "octo/app")])
def test_github_whoami_command(argv: list[str], repo: str | None) -> None:
    from chirpo.cli import run_github

    args = build_parser().parse_args(["github", "whoami", *argv])
    assert (args.handler, args.repo) == (run_github, repo)


@pytest.mark.parametrize(
    ("repo", "message"), [(None, "CHIRPO_GITHUB_APP_ID"), ("not-a-repo", "owner/repo")]
)
def test_github_whoami_explains_setup_problems(
    monkeypatch: pytest.MonkeyPatch, repo: str | None, message: str
) -> None:
    import chirpo.cli
    from chirpo.config import Settings
    from chirpo.security import generate_key

    for name in [name for name in os.environ if name.startswith("CHIRPO_")]:
        monkeypatch.delenv(name)
    unconfigured = Settings(_env_file=None, encryption_key=generate_key())  # type: ignore[call-arg]
    monkeypatch.setattr(chirpo.cli, "get_settings", lambda: unconfigured)
    with pytest.raises(SystemExit, match=message):
        chirpo.cli.run_github(
            build_parser().parse_args(["github", "whoami", *([repo] if repo else [])])
        )


def test_github_demo_command() -> None:
    from chirpo.cli import run_github

    args = build_parser().parse_args(["github", "demo", "octo/app", "--write"])
    assert (args.handler, args.repo, args.write) == (run_github, "octo/app", True)
    assert not build_parser().parse_args(["github", "demo", "octo/app"]).write


def test_github_demo_needs_a_repo() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["github", "demo"])


def _unconfigured(monkeypatch: pytest.MonkeyPatch, **values: object) -> object:
    from chirpo.config import Settings
    from chirpo.security import generate_key

    for name in [name for name in os.environ if name.startswith("CHIRPO_")]:
        monkeypatch.delenv(name)
    return Settings(_env_file=None, encryption_key=generate_key(), **values)  # type: ignore[call-arg]


def test_llm_try_command() -> None:
    from chirpo.cli import run_llm

    args = build_parser().parse_args(["llm", "try", "checkout crashes", "--sender", "Paul"])
    assert (args.handler, args.text, args.sender) == (run_llm, "checkout crashes", "Paul")
    assert build_parser().parse_args(["llm", "try", "hi"]).sender == "Franck"


def test_llm_try_needs_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import chirpo.cli

    settings = _unconfigured(monkeypatch)
    monkeypatch.setattr(chirpo.cli, "get_settings", lambda: settings)
    with pytest.raises(SystemExit, match="CHIRPO_LLM_API_KEY"):
        chirpo.cli.run_llm(build_parser().parse_args(["llm", "try", "hi"]))


def test_llm_try_prints_the_proposals(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import chirpo.adapters.llm
    import chirpo.cli
    from chirpo.core.actions import CreateIssue, NotARequest
    from chirpo.core.llm import Interpretation, InterpretRequest

    seen: list[InterpretRequest] = []

    class Fake:
        model = "fake-model"

        async def interpret(self, request: InterpretRequest) -> Interpretation:
            seen.append(request)
            proposals = (CreateIssue(title="Checkout crashes", labels=("bug",)), NotARequest())
            return Interpretation(proposals, "fake-model-001", 1800, 90)

    settings = _unconfigured(monkeypatch)
    monkeypatch.setattr(chirpo.cli, "get_settings", lambda: settings)
    monkeypatch.setattr(chirpo.adapters.llm, "llm_from_settings", lambda _settings: Fake())
    chirpo.cli.run_llm(build_parser().parse_args(["llm", "try", "checkout crashes"]))
    out = capsys.readouterr().out
    assert "From: Franck\ncheckout crashes" in seen[0].message
    assert "create_issue (needs confirmation, permission triage_issue)" in out
    assert '"title": "Checkout crashes"' in out
    assert '"labels": [\n    "bug"\n  ]' in out
    assert "not_a_request (a reply, nothing on GitHub)" in out
    assert "fake-model-001: 1800 tokens in, 90 out" in out


def test_llm_try_reports_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import chirpo.adapters.llm
    import chirpo.cli
    from chirpo.core.errors import LLMRateLimitedError
    from chirpo.core.llm import Interpretation, InterpretRequest

    class Limited:
        model = "fake-model"

        async def interpret(self, request: InterpretRequest) -> Interpretation:
            raise LLMRateLimitedError(17)

    settings = _unconfigured(monkeypatch)
    monkeypatch.setattr(chirpo.cli, "get_settings", lambda: settings)
    monkeypatch.setattr(chirpo.adapters.llm, "llm_from_settings", lambda _settings: Limited())
    with pytest.raises(SystemExit, match="rate limited"):
        chirpo.cli.run_llm(build_parser().parse_args(["llm", "try", "hi"]))


def test_llm_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from chirpo.adapters.llm import llm_from_settings
    from chirpo.adapters.llm.gemini import DEFAULT_MODEL, GeminiProvider

    assert llm_from_settings(_unconfigured(monkeypatch)) is None  # type: ignore[arg-type]
    provider = llm_from_settings(_unconfigured(monkeypatch, llm_api_key="k"))  # type: ignore[arg-type]
    assert isinstance(provider, GeminiProvider)
    assert provider.model == DEFAULT_MODEL
    chosen = llm_from_settings(
        _unconfigured(monkeypatch, llm_api_key="k", llm_model="gemini-3.5-flash-lite")  # type: ignore[arg-type]
    )
    assert chosen is not None
    assert chosen.model == "gemini-3.5-flash-lite"


def test_a_blank_llm_key_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from chirpo.adapters.llm import llm_from_settings

    assert llm_from_settings(_unconfigured(monkeypatch, llm_api_key="  ")) is None  # type: ignore[arg-type]


def test_llm_try_shows_googles_explanation(monkeypatch: pytest.MonkeyPatch) -> None:
    import chirpo.adapters.llm
    import chirpo.cli
    from chirpo.core.errors import LLMSetupError
    from chirpo.core.llm import Interpretation, InterpretRequest

    class Rejecting:
        model = "fake-model"

        async def interpret(self, request: InterpretRequest) -> Interpretation:
            raise LLMSetupError("Gemini rejected the request (400)", detail='Unknown name "x"')

    settings = _unconfigured(monkeypatch)
    monkeypatch.setattr(chirpo.cli, "get_settings", lambda: settings)
    monkeypatch.setattr(chirpo.adapters.llm, "llm_from_settings", lambda _settings: Rejecting())
    with pytest.raises(SystemExit, match='rejected the request \\(400\\): Unknown name "x"'):
        chirpo.cli.run_llm(build_parser().parse_args(["llm", "try", "hi"]))
