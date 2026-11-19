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

# Preserve protocol signals, never arbitrary provider-supplied identifiers.
_OPENAI_ERROR_CODES = frozenset({
    "context_length_exceeded", "request_too_large", "payload_too_large",
    "invalid_request_error", "invalid_value", "unsupported_value", "invalid_prompt",
    "model_not_found", "content_policy_violation", "rate_limit_exceeded",
    "insufficient_quota", "usage_limit_reached", "server_error",
})
_OPENAI_ERROR_PARAMS = frozenset({
    "input", "messages", "model", "instructions", "tools", "tool_choice",
    "max_output_tokens", "reasoning", "reasoning.effort", "previous_response_id",
    "stream", "text", "temperature", "top_p",
})


def openai_upstream_error_fields(body: bytes, *, max_bytes: int) -> tuple[str | None, str | None]:
    """Extract known scalar fields only from a complete, bounded JSON error."""
    if len(body) >= max_bytes:
        return None, None
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return None, None
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(error, dict):
        return None, None
    code, param = error.get("code"), error.get("param")
    return (
        code if isinstance(code, str) and code in _OPENAI_ERROR_CODES else None,
        param if isinstance(param, str) and param in _OPENAI_ERROR_PARAMS else None,
    )


def openai_payload_error(
    *, origin: str, request_id: str, terminal: bool, limit_bytes: int | None = None,
) -> JSONResponse:
    """A byte overflow is not a token-context overflow, even in compatibility mode."""
    message = "Request body too large."
    if limit_bytes is not None:
        message += f" Relay upload limit: {limit_bytes} bytes."
    else:
        message += " Upstream rejected the request with HTTP 413."
    message += (
        " Reduce the request before retrying. Compact earlier; if compaction also fails, "
        "start a new conversation with a concise handoff."
    )
    return openai_json_error(
        "invalid_request_error", message, status=400 if terminal else 413,
        code="request_too_large",
        headers={
            "X-Relay-Error-Origin": origin,
            "X-Relay-Original-Status": "413",
            "X-Relay-Request-Id": request_id,
        },
    )


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
