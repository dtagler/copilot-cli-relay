"""Starlette app exposing Claude and Codex relay routes."""
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route

from .claude_proxy import claude_healthz, proxy_claude_messages, proxy_claude_models
from .codex_proxy import (
    codex_healthz,
    proxy_codex_models,
    proxy_codex_responses,
    proxy_codex_responses_compact,
)
from .config import get_settings
from .logging_setup import configure_logging, logger
from .security import LocalBrowserGuardMiddleware, LoopbackHostMiddleware


@asynccontextmanager
async def lifespan(app: Starlette):
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info(
        "copilot-cli-relay starting api_base=%s integration_id=%s codex_integration_id=%s editor=%s log_bodies=%s",
        settings.api_base,
        settings.integration_id,
        settings.codex_integration_id,
        settings.editor_version,
        settings.log_bodies,
    )
    # trust_env=False so the proxy ignores host HTTP(S)_PROXY / NO_PROXY env
    # vars. Otherwise a corp/CI environment with a transparent proxy would
    # silently route Copilot bearer-token traffic through that intermediary.
    # If you ever genuinely need an outbound proxy, plumb it through an
    # explicit Settings field rather than re-enabling env discovery.
    app.state.http_client = httpx.AsyncClient(http2=True, trust_env=False)
    try:
        yield
    finally:
        await app.state.http_client.aclose()


app = Starlette(
    debug=False,
    middleware=[
        Middleware(LoopbackHostMiddleware),
        Middleware(LocalBrowserGuardMiddleware),
    ],
    routes=[
        Route("/claude/v1/messages", proxy_claude_messages, methods=["POST"]),
        Route("/claude/v1/models", proxy_claude_models, methods=["GET"]),
        Route("/codex/v1/responses", proxy_codex_responses, methods=["POST"]),
        Route("/codex/v1/responses/compact", proxy_codex_responses_compact, methods=["POST"]),
        Route("/codex/v1/models", proxy_codex_models, methods=["GET"]),
        Route("/claude/healthz", claude_healthz, methods=["GET"]),
        Route("/codex/healthz", codex_healthz, methods=["GET"]),
        Route("/healthz", claude_healthz, methods=["GET"]),
    ],
    lifespan=lifespan,
)
