"""The `chirpo` command: api, worker, poll, telegram, github, llm, migrate."""

import argparse
import asyncio
import contextlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from chirpo.config import get_settings

if TYPE_CHECKING:
    from chirpo.adapters.codehost.github.auth import GitHubAuth
    from chirpo.core.domain import RepoRef

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


def run_github(args: argparse.Namespace) -> None:
    asyncio.run(_github(args))


async def _github(args: argparse.Namespace) -> None:
    from chirpo.adapters.codehost.github.auth import GitHubAuth, GitHubSetupError
    from chirpo.core.domain import RepoRef
    from chirpo.core.errors import CodeHostError

    try:
        repo = RepoRef.parse("github", args.repo) if args.repo else None
        auth = GitHubAuth.from_settings(get_settings())
        if auth is None:
            raise SystemExit(
                "Set CHIRPO_GITHUB_APP_ID and CHIRPO_GITHUB_APP_PRIVATE_KEY_PATH in your .env "
                "(or CHIRPO_GITHUB_DEV_TOKEN in development)."
            )
        await _github_whoami(auth, repo)
        if args.github_command == "demo" and repo is not None:
            await _github_demo(auth, repo, write=args.write)
    except (ValueError, GitHubSetupError, CodeHostError) as exc:
        raise SystemExit(str(exc)) from exc


async def _github_whoami(auth: "GitHubAuth", repo: "RepoRef | None") -> None:
    identity = await auth.whoami()
    label = "GitHub App" if identity.kind == "app" else "development token of"
    print(f"Logged in as {label} {identity.name}")
    if repo is not None:
        access = await auth.check_repo(repo)
        visibility = "private" if access.private else "public"
        print(f"Can reach {access.full_name} ({visibility} repo)")
        for name, level in sorted(access.permissions.items()):
            print(f"  {name}: {level}")


async def _github_demo(auth: "GitHubAuth", repo: "RepoRef", *, write: bool) -> None:
    """The M2 acceptance check: everything chirpo does on GitHub, run once against a real repo."""
    from chirpo.adapters.codehost.github.adapter import GitHubAdapter
    from chirpo.core.domain import (
        CloseReason,
        IssuePatch,
        IssueQuery,
        IssueState,
        NewIssue,
        PullRequestQuery,
    )

    github = GitHubAdapter(auth)
    issues = await github.list_issues(repo, IssueQuery(limit=5))
    print(f"\nOpen issues: {len(issues)}{' (first 5)' if len(issues) == 5 else ''}")
    for issue in issues:
        assigned = ", ".join(issue.assignees) or "nobody"
        print(f"  #{issue.number} {issue.title} (assigned: {assigned})")

    pulls = await github.list_pull_requests(repo, PullRequestQuery(limit=5))
    print(f"\nOpen pull requests: {len(pulls)}{' (first 5)' if len(pulls) == 5 else ''}")
    for pull in pulls:
        merge = {True: "mergeable", False: "has conflicts", None: "mergeability unknown"}
        draft = "draft, " if pull.draft else ""
        closes = "".join(f", closes #{number}" for number in pull.closes_issues)
        print(
            f"  !{pull.number} {pull.title} ({draft}CI {pull.checks}, "
            f"{pull.approvals} approval(s), {merge[pull.mergeable]}{closes})"
        )

    if not write:
        print("\nRead-only run. Add --write to also create, comment on and close a test issue.")
        return
    created = await github.create_issue(
        repo,
        NewIssue(
            title="chirpo demo issue",
            body="Created by `chirpo github demo --write`. Safe to delete.",
        ),
    )
    print(f"\nCreated #{created.number}: {created.url}")
    await github.add_comment(repo, created.number, "A comment from chirpo's demo.")
    print("Commented on it")
    closed = await github.update_issue(
        repo,
        created.number,
        IssuePatch(state=IssueState.CLOSED, close_reason=CloseReason.NOT_PLANNED),
    )
    print(f"Closed it ({closed.close_reason})")


def run_llm(args: argparse.Namespace) -> None:
    asyncio.run(_llm_try(args.text, args.sender))


async def _llm_try(text: str, sender: str) -> None:
    """Shows what the LLM proposes for a message, before anything reads chat or acts on GitHub."""
    from chirpo.adapters import llm as llm_adapters
    from chirpo.core.actions import permission_of, spec_of
    from chirpo.core.errors import LLMError, LLMSetupError
    from chirpo.core.llm import interpret_request

    try:
        provider = llm_adapters.llm_from_settings(get_settings())
    except LLMSetupError as exc:
        raise SystemExit(str(exc)) from exc
    if provider is None:
        raise SystemExit(
            "Set CHIRPO_LLM_API_KEY in your .env (a Gemini key from https://aistudio.google.com/apikey)."
        )
    try:
        result = await provider.interpret(interpret_request(text, sender))
    except LLMError as exc:
        detail = getattr(exc, "detail", "")
        raise SystemExit(f"{exc}: {detail}" if detail else str(exc)) from exc
    for proposal in result.proposals:
        spec = spec_of(proposal)
        permission = permission_of(proposal)
        if permission is None:
            print(f"{spec.name} (a reply, nothing on GitHub)")
        else:
            when = "needs confirmation" if spec.needs_confirmation else "runs right away"
            print(f"{spec.name} ({when}, permission {permission})")
        fields = proposal.model_dump(mode="json", exclude_defaults=True)
        if fields:
            print(json.dumps(fields, indent=2, ensure_ascii=False))
    print(f"{result.model}: {result.input_tokens} tokens in, {result.output_tokens} out")


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
    github = commands.add_parser("github", help="check the GitHub connection")
    github_commands = github.add_subparsers(dest="github_command", required=True)
    whoami = github_commands.add_parser("whoami", help="show who chirpo is on GitHub")
    whoami.add_argument("repo", nargs="?", help="owner/repo to check access to")
    demo = github_commands.add_parser(
        "demo", help="show a repo's issues and PRs as chirpo sees them"
    )
    demo.add_argument("repo", help="owner/repo")
    demo.add_argument("--write", action="store_true", help="also create and close a test issue")
    github.set_defaults(handler=run_github)
    llm = commands.add_parser("llm", help="check the LLM provider")
    llm_commands = llm.add_subparsers(dest="llm_command", required=True)
    try_ = llm_commands.add_parser("try", help="show what the LLM proposes for a message")
    try_.add_argument("text", help='the chat message, e.g. "checkout crashes, give it to Annie"')
    try_.add_argument("--sender", default="Franck", help="the sender's display name")
    llm.set_defaults(handler=run_llm)
    commands.add_parser("migrate", help="apply database migrations").set_defaults(
        handler=run_migrate
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.handler(args)
