"""Admission and upload limits, held through the entire ASGI response lifetime."""
from __future__ import annotations

import anyio
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import get_settings
from .errors import anthropic_json_error, openai_json_error


class RequestLimitsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.active = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/livez":
            await self.app(scope, receive, send)
            return
        settings = get_settings()
        error = openai_json_error if scope["path"].startswith("/codex/") else anthropic_json_error
        if self.active >= settings.max_inflight:
            response = error(
                "server_error" if scope["path"].startswith("/codex/") else "overloaded_error",
                "Relay concurrency limit reached.", status=503, headers={"Retry-After": "1"},
            )
            await response(scope, receive, send)
            return
        # No await between admission and increment: this server is single-loop.
        self.active += 1
        try:
            pending: bytes | None = None
            if scope["method"] == "POST":
                length = Headers(scope=scope).get("content-length")
                if length is not None:
                    if not length.isascii() or not length.isdigit():
                        await error("invalid_request_error", "Invalid Content-Length.", status=400)(scope, receive, send)
                        return
                    if len(length) > 12 or int(length) > settings.max_request_bytes:
                        await error("invalid_request_error", "Request body too large.", status=413)(scope, receive, send)
                        return
                body = bytearray()
                try:
                    with anyio.fail_after(settings.request_body_timeout):
                        while True:
                            message = await receive()
                            if message["type"] == "http.disconnect":
                                return
                            chunk = message.get("body", b"")
                            if len(body) + len(chunk) > settings.max_request_bytes:
                                await error("invalid_request_error", "Request body too large.", status=413)(scope, receive, send)
                                return
                            body.extend(chunk)
                            if not message.get("more_body", False):
                                break
                except TimeoutError:
                    await error("invalid_request_error", "Request upload timed out.", status=408)(scope, receive, send)
                    return
                if length is not None and len(body) != int(length):
                    await error("invalid_request_error", "Content-Length does not match body.", status=400)(scope, receive, send)
                    return
                pending = bytes(body)
                del body

            disconnected = anyio.Event()
            started = False

            async def replay() -> Message:
                nonlocal pending
                if pending is not None:
                    payload, pending = pending, None
                    return {"type": "http.request", "body": payload, "more_body": False}
                await disconnected.wait()
                return {"type": "http.disconnect"}

            async def tracked_send(message: Message) -> None:
                nonlocal started
                if message["type"] == "http.response.start":
                    started = True
                await send(message)

            async with anyio.create_task_group() as group:
                async def watch_disconnect() -> None:
                    # Sole reader after upload: no racing Starlette consumers.
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            disconnected.set()
                            group.cancel_scope.cancel()
                            return

                group.start_soon(watch_disconnect)
                with anyio.move_on_after(settings.request_timeout) as deadline:
                    await self.app(scope, replay, tracked_send)
                group.cancel_scope.cancel()
            if deadline.cancel_called and not disconnected.is_set():
                if started:
                    # Already-started streams must fail, never fabricate completion.
                    raise TimeoutError("Relay request deadline exceeded after response start.")
                await error(
                    "server_error" if scope["path"].startswith("/codex/") else "api_error",
                    "Relay request deadline exceeded.", status=504,
                )(scope, receive, send)
        finally:
            self.active -= 1
