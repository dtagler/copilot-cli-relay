"""Shared proxy HTTP utilities used by Claude and Codex routes."""
from __future__ import annotations

import anyio
import httpx
from starlette.requests import Request
from starlette.responses import Response

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
        buf.extend(chunk)
        if len(buf) >= max_bytes:
            return bytes(buf[:max_bytes])
    return bytes(buf)


async def chunks_with_keepalive(upstream: httpx.Response, request: Request):
    """Yield (chunk, sentinel) tuples from upstream, multiplexed with ping/disconnect.

    A background producer task copies bytes from ``upstream.aiter_bytes()`` into
    a bounded memory channel; the consumer loop selects between channel reads
    and the disconnect poll. This keeps ping timeouts from cancelling the
    upstream read, which would otherwise finalize the httpx async generator and
    silently truncate long-running streams.
    """
    send, recv = anyio.create_memory_object_stream(max_buffer_size=8)
    producer_error: list[BaseException] = []

    async def _producer() -> None:
        try:
            async for raw in upstream.aiter_bytes():
                await send.send(raw)
        except anyio.get_cancelled_exc_class():
            raise
        except Exception as exc:  # captured for re-raise after channel drains
            logger.warning("upstream stream pump error: %s", redact_text(str(exc)))
            producer_error.append(exc)
        finally:
            await send.aclose()

    async with anyio.create_task_group() as tg:
        tg.start_soon(_producer)
        try:
            while True:
                if await request.is_disconnected():
                    yield b"", "disconnect"
                    tg.cancel_scope.cancel()
                    return
                with anyio.move_on_after(PING_INTERVAL_SECS) as scope:
                    try:
                        chunk = await recv.receive()
                    except anyio.EndOfStream:
                        break
                if scope.cancel_called:
                    yield b"", "ping"
                    continue
                yield chunk, None
        finally:
            await recv.aclose()

    if producer_error:
        raise producer_error[0]
