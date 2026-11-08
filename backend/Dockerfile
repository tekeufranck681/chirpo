# Development image for api, worker and migrate. The hardened production image is roadmap M10.1.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.20 /uv /bin/uv

# The virtualenv lives outside /app so mounting the source over /app doesn't hide it.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

COPY . .
RUN uv sync --locked

EXPOSE 8000
CMD ["chirpo", "api", "--host", "0.0.0.0"]
