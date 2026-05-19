"""Protocol-specific error envelope helpers for Claude and Codex routes."""
from __future__ import annotations

import json

from starlette.responses import JSONResponse

ANTHROPIC_ERROR_STATUS = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "permission_error": 403,
    "not_found_error": 404,
    "rate_limit_error": 429,
    "api_error": 502,
    "overloaded_error": 503,
}


def anthropic_envelope(error_type: str, message: str) -> dict:
    return {"type": "error", "error": {"type": error_type, "message": message}}


def anthropic_json_error(
    error_type: str,
    message: str,
    *,
    status: int | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        anthropic_envelope(error_type, message),
        status_code=status or ANTHROPIC_ERROR_STATUS.get(error_type, 500),
        headers=headers,
    )


def anthropic_sse_error_event(error_type: str, message: str) -> bytes:
    payload = json.dumps(anthropic_envelope(error_type, message), separators=(",", ":"))
    return f"event: error\ndata: {payload}\n\n".encode()


OPENAI_ERROR_STATUS = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "permission_error": 403,
    "not_found_error": 404,
    "rate_limit_error": 429,
    "server_error": 502,
}


def openai_envelope(
    error_type: str,
    message: str,
    *,
    code: str | None = None,
    param: str | None = None,
) -> dict:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
        }
    }


def openai_json_error(
    error_type: str,
    message: str,
    *,
    status: int | None = None,
    headers: dict[str, str] | None = None,
    code: str | None = None,
    param: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        openai_envelope(error_type, message, code=code, param=param),
        status_code=status or OPENAI_ERROR_STATUS.get(error_type, 500),
        headers=headers,
    )


def openai_sse_error_event(error_type: str, message: str) -> bytes:
    payload = json.dumps(
        {"type": "error", "code": error_type, "message": message, "param": None},
        separators=(",", ":"),
    )
    return f"event: error\ndata: {payload}\n\n".encode()
