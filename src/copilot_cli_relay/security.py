"""Security middleware for the local relay surface."""
from __future__ import annotations

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .errors import anthropic_json_error, openai_json_error

_JSON_POST_PATHS = frozenset({
    "/claude/v1/messages",
    "/codex/v1/responses",
    "/codex/v1/responses/compact",
})
_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_LOOPBACK_ORIGIN_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_BLOCKED_SEC_FETCH_SITES = frozenset({"cross-site"})


def _host_without_port(value: str | None) -> str | None:
    if not value:
        return None
    host = value.strip()
    if not host:
        return None
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return None
        rest = host[end + 1:]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return None
        return host[1:end].lower()
    if host.count(":") > 1:
        return host.lower()
    if ":" in host:
        name, port = host.rsplit(":", 1)
        if not port.isdigit():
            return None
        return name.lower()
    return host.lower()


def _is_allowed_host(value: str | None) -> bool:
    return _host_without_port(value) in _ALLOWED_HOSTS


def _is_loopback_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.hostname in _LOOPBACK_ORIGIN_HOSTS


def _is_json_content_type(value: str | None) -> bool:
    if not value:
        return False
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type == "application/json" or (
        media_type.startswith("application/") and media_type.endswith("+json")
    )


def _security_error(path: str, error_type: str, message: str, *, status: int) -> Response:
    if path.startswith("/codex/"):
        return openai_json_error(error_type, message, status=status)
    if path.startswith("/claude/") or path == "/healthz":
        return anthropic_json_error(error_type, message, status=status)
    return JSONResponse({"error": message}, status_code=status)


class LocalBrowserGuardMiddleware:
    """Reject browser-originated cross-site writes before the Copilot token is used."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        path = str(scope.get("path", ""))
        method = str(scope.get("method", "")).upper()

        origin = headers.get("origin")
        if origin and not _is_loopback_origin(origin):
            response = _security_error(
                path,
                "permission_error",
                "Browser-originated cross-site requests are not allowed.",
                status=403,
            )
            await response(scope, receive, send)
            return

        sec_fetch_site = headers.get("sec-fetch-site", "").strip().lower()
        if sec_fetch_site in _BLOCKED_SEC_FETCH_SITES:
            response = _security_error(
                path,
                "permission_error",
                "Browser-originated cross-site requests are not allowed.",
                status=403,
            )
            await response(scope, receive, send)
            return

        if method == "POST" and path in _JSON_POST_PATHS and not _is_json_content_type(
            headers.get("content-type")
        ):
            response = _security_error(
                path,
                "invalid_request_error",
                "POST requests to the relay must use Content-Type: application/json.",
                status=415,
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


class LoopbackHostMiddleware:
    """Allow only explicit loopback Host headers, including bracketed IPv6."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        path = str(scope.get("path", ""))
        if not _is_allowed_host(headers.get("host")):
            response = _security_error(
                path,
                "permission_error",
                "Host header must be localhost, 127.0.0.1, or [::1].",
                status=403,
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
