FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

WORKDIR /app

# Install deps first for layer caching. Lockfile is required — fail fast on
# drift rather than silently regenerating.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Copy source
COPY src ./src
COPY tests ./tests
# Project installed in editable mode (default for `uv sync`) so the venv's
# console scripts (e.g. /app/.venv/bin/uvicorn) load from /app/src — which
# the docker-compose bind mount overlays with the host's working copy.
# Without this, "edit src/, docker compose restart" silently runs stale code.
RUN uv sync --frozen

# Non-root user
RUN useradd --uid 1000 --create-home --shell /bin/bash app \
 && chown -R app:app /app
USER app

EXPOSE 4141

# Invoke the venv's uvicorn binary directly so it becomes PID 1. Going through
# `uv run uvicorn ...` would put `uv` at PID 1; on `docker stop` SIGTERM goes
# to uv, and whether it forwards the signal cleanly to uvicorn (so in-flight
# streams drain instead of being SIGKILL'd after the 10s grace window) is not
# a guarantee we want to depend on.
CMD ["/app/.venv/bin/uvicorn", "claude_copilot_cli_relay.server:app", "--host", "0.0.0.0", "--port", "4141"]
