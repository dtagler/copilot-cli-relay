"""Local liveness and per-protocol, catalog-based readiness. No inference."""
from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .claude_proxy import proxy_claude_models
from .codex_proxy import proxy_codex_models


async def livez(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "version": __version__})


async def readyz(request: Request) -> JSONResponse:
    codex = request.url.path.startswith("/codex/")
    response = await (proxy_codex_models(request) if codex else proxy_claude_models(request))
    selected = request.query_params.get("model")
    count = 0
    ready = False
    if response.status_code == 200:
        payload = json.loads(response.body)
        data = payload["data"]
        ids = {row["id"] for row in data}
        if codex:
            ids &= {row["slug"] for row in payload["models"]}
        count = len(ids)
        ready = bool(ids) and (selected is None or selected in ids)
    # Never echo request parameters, upstream bodies or credentials.
    return JSONResponse({
        "ready": ready, "selectable_models": count,
        "catalog_status": response.status_code, "inference_verified": False,
    }, status_code=200 if ready else 503)
