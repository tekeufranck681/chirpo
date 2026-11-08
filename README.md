# chirpo

Manage your GitHub from your team chat, in plain language.

> **Status: early development.** Nothing works end to end yet. Watch the repo to follow progress.

In a Telegram group, someone writes:

> @chirpo checkout crashes on Android 13, give it to Paul, it's urgent

chirpo proposes a GitHub issue (title, assignee, labels, priority), waits for confirmation, creates it,
and replies with the link. It also posts pull request and issue activity back into the chat.

- **Self-hosted** with `docker compose up`, or hosted (later).
- **Safe by design:** the language model only *proposes* actions. Code validates them, checks permissions,
  and asks for confirmation before anything happens on GitHub.
- **Pluggable:** Telegram and GitHub first; Slack, WhatsApp, GitLab and Linear later. Gemini is the
  default LLM; other providers can be added.

## Development

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), Docker.

The repo has three parts: `backend/` (Python API and worker), `dashboard/` (React web app, coming
later), and `deploy/` (Docker Compose and the Caddy web server, coming later).

First time (Docker commands run from the repo root; `COMPOSE_FILE` in `.env` makes `docker compose`
mean `docker compose -f docker-compose.dev.yml`):

```bash
cp .env.example .env            # then set CHIRPO_ENCRYPTION_KEY (the file explains how)
docker compose up -d            # builds and starts everything, runs migrations
curl localhost:8080/api/ready   # {"status":"ok",...} once it's up
cd backend                      # for local tooling (tests, linters)
uv sync                         # install dependencies
uv run pre-commit install       # run checks before each commit (once per clone)
uv run alembic upgrade head     # create the database tables
```

What runs (all on localhost only):

| Address | Service |
|---|---|
| http://localhost:8080 | Caddy, the entry point (`/api/*` and `/webhooks/*` go to the API) |
| http://localhost:8080/api/docs | API documentation |
| http://localhost:8081 | Adminer, database GUI (server `db`) |
| localhost:5433 / 6380 | Postgres / Redis, for local tools |

The API and worker reload automatically when you edit `backend/src`. Logs: `docker compose logs -f api worker`.

Everyday commands (from `backend/`):

```bash
uv run pytest                   # all tests (database tests need `docker compose up -d`)
uv run pytest tests/unit        # fast tests only, no database needed
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run alembic revision --autogenerate -m "describe the change"   # after changing models
```

Pull request titles follow [Conventional Commits](https://www.conventionalcommits.org/)
with a scope: `type(scope): message`, e.g. `feat(llm): add Gemini provider`.

## License

[MIT](LICENSE)
