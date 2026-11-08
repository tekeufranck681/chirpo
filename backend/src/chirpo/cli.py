"""The `chirpo` command: `chirpo api`, `chirpo worker`, `chirpo migrate`."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from chirpo.config import get_settings

SOURCE_DIR = Path(__file__).resolve().parent
ALEMBIC_INI = SOURCE_DIR.parents[1] / "alembic.ini"


def run_api(args: argparse.Namespace) -> None:
    import uvicorn

    from chirpo.logging import configure_logging

    configure_logging(get_settings())
    uvicorn.run(
        "chirpo.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(SOURCE_DIR)] if args.reload else None,
        log_config=None,  # keep our structlog setup instead of uvicorn's own
        proxy_headers=True,
    )


def run_worker(_args: argparse.Namespace) -> None:
    from arq.connections import RedisSettings
    from arq.worker import run_worker as run_arq_worker

    from chirpo.workers.main import WorkerSettings

    run_arq_worker(
        WorkerSettings,  # type: ignore[arg-type]
        redis_settings=RedisSettings.from_dsn(get_settings().redis_url),
    )


def run_migrate(_args: argparse.Namespace) -> None:
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(ALEMBIC_INI), "head")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chirpo")
    commands = parser.add_subparsers(dest="command", required=True)

    api = commands.add_parser("api", help="run the HTTP API")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    api.add_argument("--reload", action="store_true", help="restart on code changes (dev)")
    api.set_defaults(handler=run_api)

    commands.add_parser("worker", help="run the background job worker").set_defaults(
        handler=run_worker
    )
    commands.add_parser("migrate", help="apply database migrations").set_defaults(
        handler=run_migrate
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.handler(args)
