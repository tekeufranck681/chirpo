"""The `chirpo` command: `chirpo api`, `chirpo worker`, `chirpo poll`, `chirpo migrate`."""

import argparse
import asyncio
import contextlib
import json
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
        forwarded_allow_ips=get_settings().trusted_proxies,
    )


def run_worker(_args: argparse.Namespace) -> None:
    from arq.connections import RedisSettings
    from arq.worker import run_worker as run_arq_worker

    from chirpo.workers.main import WorkerSettings

    run_arq_worker(
        WorkerSettings,  # type: ignore[arg-type]
        redis_settings=RedisSettings.from_dsn(get_settings().redis_url),
    )


def run_poll(_args: argparse.Namespace) -> None:
    # Ctrl-C / watchfiles reload: stop quietly. The offset is saved per update, so nothing is lost.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_poll())


async def _poll() -> None:
    from arq import create_pool
    from arq.connections import RedisSettings

    from chirpo.adapters.messaging.telegram.adapter import TelegramAdapter
    from chirpo.adapters.messaging.telegram.poller import run_poller
    from chirpo.db.repositories import SqlPollCursors
    from chirpo.db.session import create_engine, create_session_factory
    from chirpo.logging import configure_logging, get_logger, init_sentry
    from chirpo.workers.queue import update_enqueuer

    settings = get_settings()
    configure_logging(settings)
    init_sentry(settings)
    log = get_logger("chirpo.poll")
    if settings.telegram_mode != "polling":
        log.info("telegram_polling_disabled", mode=settings.telegram_mode)
        return
    if settings.telegram_bot_token is None:
        log.warning("telegram_not_configured", hint="set CHIRPO_TELEGRAM_BOT_TOKEN in .env")
        return

    adapter = await TelegramAdapter.connect(settings.telegram_bot_token.get_secret_value())
    engine = create_engine(settings)
    queue = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    cursors = SqlPollCursors(create_session_factory(engine))

    try:
        await run_poller(adapter, cursors, update_enqueuer(queue, adapter))
    finally:
        await queue.aclose()
        await engine.dispose()
        await adapter.aclose()


def run_telegram(args: argparse.Namespace) -> None:
    asyncio.run(_telegram(args.telegram_command))


async def _telegram(action: str) -> None:
    from chirpo.adapters.messaging.telegram.adapter import TelegramAdapter

    settings = get_settings()
    if settings.telegram_bot_token is None:
        raise SystemExit("Set CHIRPO_TELEGRAM_BOT_TOKEN in your .env first.")
    adapter = await TelegramAdapter.connect(settings.telegram_bot_token.get_secret_value())
    try:
        if action == "set-webhook":
            if settings.public_url is None or settings.telegram_webhook_secret is None:
                raise SystemExit(
                    "Set CHIRPO_PUBLIC_URL and CHIRPO_TELEGRAM_WEBHOOK_SECRET in your .env first."
                )
            url = f"{str(settings.public_url).rstrip('/')}/webhooks/telegram"
            await adapter.set_webhook(url, settings.telegram_webhook_secret.get_secret_value())
            print(f"Telegram will now send updates to {url}")
        elif action == "delete-webhook":
            await adapter.delete_webhook()
            print("Webhook removed: polling mode can be used again.")
        else:
            print(json.dumps(await adapter.webhook_info(), indent=2, default=str))
    finally:
        await adapter.aclose()


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
    commands.add_parser("poll", help="receive Telegram updates by long polling").set_defaults(
        handler=run_poll
    )
    telegram = commands.add_parser("telegram", help="manage the Telegram webhook")
    telegram.add_argument(
        "telegram_command", choices=["set-webhook", "delete-webhook", "webhook-info"]
    )
    telegram.set_defaults(handler=run_telegram)
    commands.add_parser("migrate", help="apply database migrations").set_defaults(
        handler=run_migrate
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.handler(args)
