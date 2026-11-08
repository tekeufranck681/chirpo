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
