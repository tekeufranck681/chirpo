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
