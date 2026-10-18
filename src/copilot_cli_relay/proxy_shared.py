"""Shared proxy HTTP utilities used by Claude and Codex routes."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import aclosing, suppress

import anyio
import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from .logging_setup import logger, redact_text

PING_INTERVAL_SECS = 15.0
UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)

# RFC 7230 section 6.1 hop-by-hop headers (lowercased) must be stripped from
# responses we forward to local clients. Also drop framing headers httpx will
# recompute (content-length, content-encoding, transfer-encoding).
_HOP_BY_HOP_RESPONSE_HEADERS = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
    "set-cookie",
    "server",
})

# Cap on how many bytes of an upstream error body we read before returning an
# error envelope. Bounds memory under hostile/misbehaving upstreams.
MAX_UPSTREAM_ERROR_BYTES = 32 * 1024


def _start_cleanup(operation: Callable[[], Awaitable[None]]) -> asyncio.Task[BaseException | None]:
    async def run() -> BaseException | None:
        try:
            await operation()
        except BaseException as exc:
            # Python can log an inner shielded task's exception after its
            # caller cancels, even if another waiter retrieves that exception.
            # Return a sanitized failure as a value, not a failed Future.
            return RuntimeError(redact_text(str(exc))) if isinstance(exc, Exception) else exc
        return None

    return asyncio.create_task(run(), name="copilot-relay-stream-cleanup")


async def _await_cleanup(task: asyncio.Task[BaseException | None]) -> None:
    """Finish cleanup despite cancellation, then propagate caller cancellation.

    AnyIO shielding alone does not block direct asyncio.Task.cancel(). Shield
    the cleanup task too, and do not let a caller cancellation cancel the task
    being joined or get mistaken for the producer's intentional cancellation.
    """
    interrupted: asyncio.CancelledError | None = None
    cleanup_error: BaseException | None = None
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if interrupted is None:
                    interrupted = exc
        try:
            cleanup_error = task.result()
        except BaseException as exc:
            cleanup_error = exc
    if interrupted is not None:
        if cleanup_error is not None and not isinstance(cleanup_error, asyncio.CancelledError):
            logger.warning("stream cleanup failed during cancellation err=%s", redact_text(str(cleanup_error)))
        raise interrupted
    # Deliver AnyIO level cancellation that was postponed by the shield.
    await anyio.lowlevel.checkpoint_if_cancelled()
    if cleanup_error is not None:
        raise cleanup_error from None


class _CancellationSafeStream(httpx.AsyncByteStream):
    """Protect the first transport close, including HTTPX's automatic EOF close."""

    def __init__(self, inner: httpx.AsyncByteStream) -> None:
        self._inner = inner
        self._close_task: asyncio.Task[BaseException | None] | None = None

    async def __aiter__(self):
        async for chunk in self._inner:
            yield chunk

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = _start_cleanup(self._inner.aclose)
        await _await_cleanup(self._close_task)


class UpstreamStreamingResponse(StreamingResponse):
    """Close acquired upstream resources even if sending headers/body fails."""

    def __init__(
        self,
        content: AsyncGenerator[bytes],
        *,
        upstream: httpx.Response,
        headers: Mapping[str, str],
    ) -> None:
        super().__init__(content, media_type="text/event-stream", headers=headers)
        self._content = content
        self._upstream = upstream
        # HTTPX sets response.is_closed BEFORE awaiting stream.aclose(). Merely
        # retrying response.aclose() after cancellation can therefore do nothing.
        # Wrap before consumption so the producer's automatic EOF close is safe.
        upstream.stream = _CancellationSafeStream(upstream.stream)

    async def _close(self) -> None:
        try:
            await self._content.aclose()
        finally:
            await self._upstream.aclose()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # The body might never have started, or be suspended at a yield
            # after a downstream send failure. Its finally alone is insufficient.
            await _await_cleanup(_start_cleanup(self._close))


class _SSEBoundary:
    """Constant-space framing state; never buffer or rewrite upstream events."""

    def __init__(self) -> None:
        self._line_has_content = False
        self._after_cr = False
        self._between_events = True

    @property
    def can_ping(self) -> bool:
        # A CR may be the first half of a CRLF split across network chunks.
        return self._between_events and not self._after_cr

    def feed(self, chunk: bytes) -> None:
        for byte in chunk:
            if self._after_cr:
                self._after_cr = False
                if byte == 10:
                    continue
            if byte in (10, 13):
                self._between_events = not self._line_has_content
                self._line_has_content = False
                self._after_cr = byte == 13
            else:
                self._line_has_content = True
                self._between_events = False


def filter_response_headers(
    headers: httpx.Headers,
    also_drop: set[str] | None = None,
) -> dict[str, str]:
    """Filter upstream response headers down to what is safe to forward."""
    dynamic_strip: set[str] = set()
    conn_value = headers.get("connection")
    if conn_value:
        dynamic_strip.update(t.strip().lower() for t in conn_value.split(",") if t.strip())
    extra = {n.lower() for n in (also_drop or set())}

    out: dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in _HOP_BY_HOP_RESPONSE_HEADERS or lk in dynamic_strip or lk in extra:
            continue
        out[k] = v
    return out


def passthrough_response(resp: httpx.Response) -> Response:
    dynamic_strip: set[str] = set()
    conn_value = resp.headers.get("connection")
    if conn_value:
        dynamic_strip.update(t.strip().lower() for t in conn_value.split(",") if t.strip())

    # Preserve repeated header values (for example Vary, Link, WWW-Authenticate)
    # by iterating raw headers instead of dict-collapsing through .items().
    raw_headers: list[tuple[bytes, bytes]] = []
    for k, v in resp.headers.raw:
        lname = k.decode("latin-1").lower()
        if lname in _HOP_BY_HOP_RESPONSE_HEADERS or lname in dynamic_strip:
            continue
        raw_headers.append((k.lower(), v))
    raw_headers.append((b"content-length", str(len(resp.content)).encode("latin-1")))
    out = Response(content=resp.content, status_code=resp.status_code)
    out.raw_headers = raw_headers
    return out


async def read_bounded(resp: httpx.Response, max_bytes: int) -> bytes:
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf.extend(chunk[:max_bytes - len(buf)])
        if len(buf) >= max_bytes:
            return bytes(buf)
    return bytes(buf)


async def chunks_with_keepalive(upstream: httpx.Response, request: Request):
    """Yield (chunk, sentinel) tuples from upstream, multiplexed with ping/disconnect.

    A background producer task copies bytes from ``upstream.aiter_bytes()`` into
    a bounded memory channel; the consumer loop selects between channel reads
    and the disconnect poll. This keeps ping timeouts from cancelling the
    upstream read, which would otherwise finalize the httpx async generator and
    silently truncate long-running streams. Heartbeats are emitted only between
    complete SSE events. Callers that may stop early must explicitly close this
    iterator (for example with contextlib.aclosing).
    """
    send, recv = anyio.create_memory_object_stream(max_buffer_size=8)
    producer_error: list[BaseException] = []
    boundary = _SSEBoundary()
    disconnected = False

    async def _producer() -> None:
        try:
            async with aclosing(upstream.aiter_bytes()) as raw_chunks:
                async for raw in raw_chunks:
                    await send.send(raw)
        except anyio.get_cancelled_exc_class():
            raise
        except Exception as exc:  # captured for re-raise after channel drains
            logger.warning("upstream stream pump error: %s", redact_text(str(exc)))
            producer_error.append(exc)
        finally:
            await send.aclose()

    # This server is asyncio-based. An explicitly joined task avoids leaving an
    # AnyIO task-group cancel scope open across yields to a different consumer.
    producer = asyncio.create_task(_producer(), name="copilot-relay-stream-producer")

    async def stop_producer() -> None:
        producer.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await producer
        finally:
            # A task canceled before its first step never runs _producer's
            # finally. The owner must close both endpoints in that case.
            try:
                await send.aclose()
            finally:
                await recv.aclose()

    try:
        while True:
            if await request.is_disconnected():
                disconnected = True
                break
            with anyio.move_on_after(PING_INTERVAL_SECS) as scope:
                try:
                    chunk = await recv.receive()
                except anyio.EndOfStream:
                    break
            if scope.cancel_called:
                if boundary.can_ping:
                    yield b"", "ping"
                continue
            boundary.feed(chunk)
            yield chunk, None
    finally:
        await _await_cleanup(_start_cleanup(stop_producer))

    if disconnected:
        # No producer or live cancel scope remains when the consumer returns.
        yield b"", "disconnect"
        return
    if producer_error:
        raise producer_error[0]
