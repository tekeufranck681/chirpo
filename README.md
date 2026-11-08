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

```bash
cd backend
uv sync                         # install dependencies
uv run pre-commit install       # run checks before each commit (once per clone)
uv run pytest                   # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

Pull request titles follow [Conventional Commits](https://www.conventionalcommits.org/)
with a scope: `type(scope): message`, e.g. `feat(llm): add Gemini provider`.

## License

[MIT](LICENSE)
