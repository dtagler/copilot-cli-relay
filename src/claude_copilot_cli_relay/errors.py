"""Anthropic-shaped error envelope helpers (JSON and SSE variants)."""
from __future__ import annotations

import json

from starlette.responses import JSONResponse

ERROR_TYPES = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "permission_error": 403,
    "not_found_error": 404,
    "rate_limit_error": 429,
    "api_error": 502,
    "overloaded_error": 503,
}


def envelope(error_type: str, message: str) -> dict:
    return {"type": "error", "error": {"type": error_type, "message": message}}


def json_error(
    error_type: str,
    message: str,
    *,
    status: int | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        envelope(error_type, message),
        status_code=status or ERROR_TYPES.get(error_type, 500),
        headers=headers,
    )


def sse_error_event(error_type: str, message: str) -> bytes:
    payload = json.dumps(envelope(error_type, message), separators=(",", ":"))
    return f"event: error\ndata: {payload}\n\n".encode()
