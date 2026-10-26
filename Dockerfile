# Multi-platform indexes, resolved on 2026-10-26. Update deliberately with tests.
FROM ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a AS uv
FROM python:3.14-slim@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8 AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
WORKDIR /app
RUN useradd --uid 1000 --create-home --shell /bin/sh app

FROM base AS builder
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv lock --check --offline && uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# Development tools never enter the production image.
FROM base AS development
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY tests ./tests
COPY scripts ./scripts
COPY docs ./docs
COPY assets ./assets
COPY skill ./skill
COPY README.md ARCHITECTURE.md GLOSSARY.md LICENSE ./
RUN uv lock --check --offline && uv sync --frozen && chown -R app:app /app
USER app
CMD ["/app/.venv/bin/uvicorn", "copilot_cli_relay.server:app", "--host", "0.0.0.0", "--port", "4141", "--timeout-graceful-shutdown", "20", "--no-access-log"]

# Default target: installed wheel and runtime dependencies only, no source mounts.
FROM base AS production
COPY --from=builder /app/.venv /app/.venv
USER app
EXPOSE 4141
CMD ["/app/.venv/bin/uvicorn", "copilot_cli_relay.server:app", "--host", "0.0.0.0", "--port", "4141", "--timeout-graceful-shutdown", "20", "--no-access-log"]
