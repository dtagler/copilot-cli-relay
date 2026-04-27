"""Starlette app exposing the three routes the proxy serves."""
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.routing import Route

from .config import get_settings
from .logging_setup import configure_logging, logger
from .proxy import healthz, proxy_messages, proxy_models


@asynccontextmanager
async def lifespan(app: Starlette):
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info(
        "claude-copilot-cli-relay starting api_base=%s integration_id=%s editor=%s log_bodies=%s",
        settings.api_base, settings.integration_id, settings.editor_version, settings.log_bodies,
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
    routes=[
        Route("/v1/messages", proxy_messages, methods=["POST"]),
        Route("/v1/models", proxy_models, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
    ],
    lifespan=lifespan,
)
