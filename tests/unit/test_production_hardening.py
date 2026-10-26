"""Production limits, readiness and explicit compatibility policy regressions."""
from __future__ import annotations

import asyncio
import gzip
import json
import tracemalloc
import zlib
from contextlib import asynccontextmanager
from dataclasses import replace

import anyio
import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

from copilot_cli_relay.claude_proxy import proxy_claude_messages, proxy_claude_models
from copilot_cli_relay.codex_proxy import (
    _codex_model_info,
    _codex_reasoning_levels,
    _model_context_window,
    proxy_codex_models,
    proxy_codex_responses,
    proxy_codex_responses_compact,
)
from copilot_cli_relay.config import ConfigError, Settings, reset_settings_for_tests
from copilot_cli_relay.headers import build_claude_outbound_headers
from copilot_cli_relay.health import livez, readyz
from copilot_cli_relay.limits import RequestLimitsMiddleware
from copilot_cli_relay.proxy_shared import (
    ResponseTooLarge,
    close_response,
    fetch_catalog,
    protect_response,
    read_complete_bounded,
)


@pytest.fixture(autouse=True)
def settings():
    value = Settings(
        proxy_port=4141, github_token="gho_fixture", api_base="https://upstream.test",
        integration_id="fixture", editor_version="fixture", log_level="info", log_bodies=False,
    )
    reset_settings_for_tests(value)
    yield value
    reset_settings_for_tests(None)


def make_app(handler):
    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            app.state.http_client = client
            yield

    return Starlette(lifespan=lifespan, middleware=[Middleware(RequestLimitsMiddleware)], routes=[
        Route("/livez", livez),
        Route("/codex/readyz", readyz),
        Route("/claude/readyz", readyz),
        Route("/codex/v1/models", proxy_codex_models),
        Route("/claude/v1/models", proxy_claude_models),
        Route("/codex/v1/responses", proxy_codex_responses, methods=["POST"]),
        Route("/claude/v1/messages", proxy_claude_messages, methods=["POST"]),
        Route("/codex/v1/responses/compact", proxy_codex_responses_compact, methods=["POST"]),
    ])


@pytest.mark.parametrize("name", [
    "COPILOT_MAX_REQUEST_BYTES", "COPILOT_MAX_RESPONSE_BYTES", "COPILOT_MAX_CATALOG_BYTES",
    "COPILOT_MAX_INFLIGHT", "COPILOT_REQUEST_BODY_TIMEOUT", "COPILOT_REQUEST_TIMEOUT", "COPILOT_MAX_CATALOG_MODELS",
])
@pytest.mark.parametrize("bad", ["0", "-1", "999999999999", "invalid"])
def test_limits_fail_closed(monkeypatch, name, bad):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_fixture")
    monkeypatch.setenv(name, bad)
    with pytest.raises(ConfigError, match=name):
        Settings.from_env()


@pytest.mark.parametrize("value", ["[]", "null", '{"model":true}', '{"model":{"unknown":true}}',
                                        '{"model":{"supports_search_tool":"true"}}', "{", " " * 65537])
def test_metadata_override_validation(monkeypatch, value):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_fixture")
    monkeypatch.setenv("COPILOT_CODEX_MODEL_OVERRIDES", value)
    with pytest.raises(ConfigError, match="COPILOT_CODEX_MODEL_OVERRIDES"):
        Settings.from_env()


def test_explicit_metadata_policy(monkeypatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_fixture")
    monkeypatch.setenv("COPILOT_CODEX_MODEL_OVERRIDES", '{"qualified-model":{"supports_search_tool":true}}')
    reset_settings_for_tests(Settings.from_env())
    assert _codex_model_info({}, "qualified-model")["supports_search_tool"] is True
    assert _codex_model_info({}, "other-model")["supports_search_tool"] is False


@pytest.mark.parametrize("value", [None, True, -1, 0, "1000000", 10_000_001])
def test_context_limit_does_not_invent_large_windows(value):
    assert _model_context_window({"capabilities": {"limits": {"context_window": value}}}) == 8192


def test_unknown_reasoning_and_features_are_not_advertised():
    assert _codex_reasoning_levels({}) == []
    model = _codex_model_info({}, "unknown")
    assert model["default_reasoning_level"] is None
    for key in ("supports_search_tool", "supports_parallel_tool_calls", "support_verbosity"):
        assert model[key] is False
    assert _codex_reasoning_levels({"capabilities": {"supports": {"reasoning_effort": ["bogus"]}}}) == []


@pytest.mark.parametrize("native_status", [200, 404, 429, 500])
def test_default_compaction_never_creates_a_synthetic_request(native_status):
    requests = []

    def upstream(request):
        requests.append(request.url.path)
        return httpx.Response(native_status, json={"object": "response.compaction", "output": []})

    with TestClient(make_app(upstream)) as client:
        response = client.post("/codex/v1/responses/compact", json={"model": "fixture", "input": "test"})
    assert response.status_code == native_status
    assert requests == ["/responses/compact"]
    if native_status == 404:
        assert "disabled" in response.json()["error"]["message"]


@pytest.mark.parametrize("path", ["/codex/v1/responses", "/claude/v1/messages"])
@pytest.mark.parametrize("streaming", [True, False])
def test_upload_size_limit_prevents_upstream_use(settings, path, streaming):
    reset_settings_for_tests(replace(settings, max_request_bytes=32))

    def forbidden(_request):
        pytest.fail("Oversized upload reached upstream.")

    with TestClient(make_app(forbidden)) as client:
        response = client.post(path, json={"model": "fixture", "stream": streaming, "input": "x" * 32})
    assert response.status_code == 413
    assert response.json()["error"]["type"] == "invalid_request_error"


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await anyio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("chunks", [[b"1234"], [b"12", b"34"]])
async def test_complete_bounded_exact_limit(chunks):
    assert await read_complete_bounded(httpx.Response(200, stream=Chunks(chunks)), 4) == b"1234"


@pytest.mark.parametrize("chunks", [[b"12345"], [b"1234", b"5"]])
async def test_complete_bounded_never_truncates_success(chunks):
    with pytest.raises(ResponseTooLarge):
        await read_complete_bounded(httpx.Response(200, stream=Chunks(chunks)), 4)


@pytest.mark.parametrize("path", ["/codex/v1/responses", "/claude/v1/messages"])
def test_oversized_success_is_closed_and_rejected(settings, path):
    reset_settings_for_tests(replace(settings, max_response_bytes=4))
    stream = Chunks([b"1234", b"5"])
    with TestClient(make_app(lambda _request: httpx.Response(200, stream=stream))) as client:
        response = client.post(path, json={"model": "fixture"})
    assert response.status_code == 502
    assert "size limit" in response.json()["error"]["message"]
    assert stream.closed


@pytest.mark.parametrize("path", ["/codex/v1/models", "/claude/v1/models"])
def test_catalog_size_is_bounded(settings, path):
    reset_settings_for_tests(replace(settings, max_catalog_bytes=4))
    stream = Chunks([b'{"data":[]}', b"unused"])
    with TestClient(make_app(lambda _request: httpx.Response(200, stream=stream))) as client:
        response = client.get(path)
    assert response.status_code == 502
    assert stream.closed


async def test_catalog_has_total_deadline_and_closes():
    stream = Chunks([b"slow"], delay=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, stream=stream),
    )) as client:
        with pytest.raises(httpx.ReadTimeout):
            await fetch_catalog(client, "https://upstream.test/models", headers={}, timeout=0.01, max_bytes=10)
    assert stream.closed


@pytest.mark.parametrize("protocol", ["codex", "claude"])
@pytest.mark.parametrize("status,rows,selected,expected", [
    (200, True, None, 200), (200, True, "fixture", 200), (200, True, "missing", 503),
    (200, False, None, 503), (401, True, None, 503), (429, True, None, 503), (500, True, None, 503),
])
def test_readiness_uses_selectable_catalog_not_just_http(protocol, status, rows, selected, expected):
    model = {"id": "fixture", "vendor": "Anthropic", "supported_endpoints": ["/responses"],
             "capabilities": {"type": "chat"}}
    with TestClient(make_app(lambda _request: httpx.Response(status, json={"data": [model] if rows else []}))) as client:
        response = client.get(f"/{protocol}/readyz", params={} if selected is None else {"model": selected})
    assert response.status_code == expected
    assert response.json()["ready"] is (expected == 200)
    assert response.json()["inference_verified"] is False


def test_liveness_never_calls_upstream():
    def forbidden(_request):
        pytest.fail("Liveness used upstream.")

    with TestClient(make_app(forbidden)) as client:
        assert client.get("/livez").json()["ok"] is True


def scope(path="/codex/v1/responses", method="POST", headers=()):
    return {"type": "http", "path": path, "method": method, "headers": list(headers)}


@pytest.mark.parametrize("chunks,status", [
    ([{"type": "http.request", "body": b"12345", "more_body": False}], 413),
    ([{"type": "http.request", "body": b"123", "more_body": True},
      {"type": "http.request", "body": b"45", "more_body": False}], 413),
    ([{"type": "http.disconnect"}], None),
])
async def test_chunked_upload_limit_and_disconnect(settings, chunks, status):
    reset_settings_for_tests(replace(settings, max_request_bytes=4))

    async def forbidden(*_args):
        pytest.fail("Invalid upload reached handler.")

    async def receive():
        return chunks.pop(0)

    output = []

    async def send(message):
        output.append(message)

    middleware = RequestLimitsMiddleware(forbidden)
    await middleware(scope(), receive, send)
    assert middleware.active == 0
    assert (output[0]["status"] if output else None) == status


async def test_upload_timeout_releases_slot(settings):
    reset_settings_for_tests(replace(settings, request_body_timeout=0.01))

    async def receive():
        await anyio.sleep(1)

    async def forbidden(*_args):
        pytest.fail("Timed-out upload reached handler.")

    messages = []

    async def send(message):
        messages.append(message)

    middleware = RequestLimitsMiddleware(forbidden)
    await middleware(scope(), receive, send)
    assert messages[0]["status"] == 408
    assert middleware.active == 0


async def test_admission_held_through_response_and_released_on_cancel(settings):
    reset_settings_for_tests(replace(settings, max_inflight=1))
    entered = asyncio.Event()

    async def handler(request_scope, _receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        if request_scope["path"] != "/livez":
            entered.set()
            await asyncio.Event().wait()
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        await asyncio.Event().wait()

    messages = []

    async def send(message):
        messages.append(message)

    middleware = RequestLimitsMiddleware(handler)
    first = asyncio.create_task(middleware(scope(method="GET"), receive, send))
    await asyncio.wait_for(entered.wait(), 1)
    await middleware(scope(method="GET"), receive, send)
    await middleware(scope("/livez", "GET"), receive, send)
    assert [m["status"] for m in messages if m["type"] == "http.response.start"] == [200, 503, 200]
    assert middleware.active == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert middleware.active == 0
    assert b"retry-after" in dict(messages[1]["headers"])


@pytest.mark.parametrize("length,status", [(b"-1", 400), (b"x", 400), (b"99999999999999", 413), (b"2", 400)])
async def test_invalid_content_length_is_rejected(length, status):
    async def forbidden(*_args):
        pytest.fail("Bad framing reached handler.")

    async def receive():
        return {"type": "http.request", "body": b"x"}

    output = []

    async def send(message):
        output.append(message)

    middleware = RequestLimitsMiddleware(forbidden)
    await middleware(scope(headers=[(b"content-length", length)]), receive, send)
    assert output[0]["status"] == status
    assert middleware.active == 0


@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
async def test_compressed_expansion_is_bounded_before_allocation(encoding, compress):
    compressed = compress(b"x" * 4_000_000)
    response = httpx.Response(200, headers={"content-encoding": encoding}, stream=Chunks([compressed]))
    tracemalloc.start()
    try:
        with pytest.raises(ResponseTooLarge):
            await read_complete_bounded(response, 1024)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1_000_000


@pytest.mark.parametrize("encoding,content,expected", [
    ("gzip", gzip.compress(b"one") + gzip.compress(b"two"), b"onetwo"),
    ("deflate", zlib.compress(b"fixture"), b"fixture"),
    ("gzip", gzip.compress(b""), b""),
])
async def test_bounded_decoder_preserves_supported_encodings(encoding, content, expected):
    chunks = [content[i:i + 3] for i in range(0, len(content), 3)]
    response = httpx.Response(200, headers={"content-encoding": encoding}, stream=Chunks(chunks))
    assert await read_complete_bounded(response, 100) == expected


@pytest.mark.parametrize("encoding,content", [
    ("br", b"x"), ("gzip", b"invalid"), ("gzip", gzip.compress(b"x")[:-4]),
    ("deflate", zlib.compress(b"x") + b"trailing"),
])
async def test_bounded_decoder_rejects_invalid_encoded_streams(encoding, content):
    with pytest.raises(httpx.DecodingError):
        await read_complete_bounded(httpx.Response(200, headers={"content-encoding": encoding}, stream=Chunks([content])), 100)


class AwaitingClose(Chunks):
    async def aclose(self):
        await asyncio.sleep(0.01)
        self.closed = True


@pytest.mark.parametrize("cancel", [False, True])
async def test_catalog_cleanup_survives_deadline_and_cancellation(cancel):
    stream = AwaitingClose([b"slow"], delay=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=stream))) as client:
        task = asyncio.create_task(fetch_catalog(client, "https://upstream.test/models", headers={},
                                                 timeout=10 if cancel else 0.02, max_bytes=100))
        if cancel:
            await asyncio.sleep(0.02)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else httpx.ReadTimeout):
            await task
    assert stream.closed


@pytest.mark.parametrize("protocol", ["codex", "claude"])
@pytest.mark.parametrize("status", [401, 403, 429])
@pytest.mark.parametrize("broken", [False, True])
def test_known_catalog_errors_survive_large_or_interrupted_bodies(settings, protocol, status, broken):
    reset_settings_for_tests(replace(settings, max_catalog_bytes=8))

    class ErrorBody(AwaitingClose):
        async def __aiter__(self):
            if broken:
                raise httpx.ReadError("synthetic failure")
            yield b"x" * 20

    stream = ErrorBody([])
    with TestClient(make_app(lambda _request: httpx.Response(status, headers={"retry-after": "3"}, stream=stream))) as client:
        response = client.get(f"/{protocol}/v1/models")
    assert response.status_code == status
    assert response.headers["retry-after"] == "3"
    assert stream.closed


@pytest.mark.parametrize("protocol", ["codex", "claude"])
@pytest.mark.parametrize("readiness", [False, True])
@pytest.mark.parametrize("kind", ["count", "bytes"])
def test_generated_catalog_and_readiness_have_budgets(settings, protocol, readiness, kind):
    reset_settings_for_tests(replace(settings, max_catalog_models=2, max_response_bytes=100 if kind == "bytes" else 10000))
    rows = [{"id": f"fixture-{i}", "vendor": "Anthropic", "supported_endpoints": ["/responses"],
             "capabilities": {"type": "chat"}} for i in range(3 if kind == "count" else 1)]
    with TestClient(make_app(lambda _request: httpx.Response(200, json={"data": rows}))) as client:
        response = client.get(f"/{protocol}/" + ("readyz" if readiness else "v1/models"))
    assert response.status_code == (503 if readiness else 502)


@pytest.mark.parametrize("protocol", ["codex", "claude"])
@pytest.mark.parametrize("deadline", [False, True])
async def test_actual_nonstream_handler_cancels_abandoned_or_timed_out_work(settings, protocol, deadline):
    reset_settings_for_tests(replace(settings, request_timeout=0.03 if deadline else 5))
    entered = asyncio.Event()
    disconnect = asyncio.Event()

    class SlowBody(AwaitingClose):
        async def __aiter__(self):
            entered.set()
            while True:
                await asyncio.sleep(0.01)
                yield b"x"  # Trickle does not hit HTTPX's idle timeout.

    stream = SlowBody([])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=stream))) as client:
        application = Starlette()
        application.state.http_client = client
        path = "/codex/v1/responses" if protocol == "codex" else "/claude/v1/messages"
        request_scope = {**scope(path), "app": application}
        route = proxy_codex_responses if protocol == "codex" else proxy_claude_messages

        async def handler(s, receive, send):
            response = await route(Request(s, receive))
            await response(s, receive, send)

        pending = True

        async def receive():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": json.dumps({"model": "fixture"}).encode()}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        messages = []

        async def send(message):
            messages.append(message)

        middleware = RequestLimitsMiddleware(handler)
        task = asyncio.create_task(middleware(request_scope, receive, send))
        await asyncio.wait_for(entered.wait(), 1)
        if not deadline:
            disconnect.set()
        await asyncio.wait_for(task, 1)
        assert stream.closed
        assert middleware.active == 0
        assert ([m["status"] for m in messages if m["type"] == "http.response.start"]) == ([504] if deadline else [])


async def test_deadline_after_headers_aborts_without_fabricated_completion(settings):
    reset_settings_for_tests(replace(settings, request_timeout=0.01))

    async def handler(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.Event().wait()

    async def receive():
        await asyncio.Event().wait()

    messages = []

    async def send(message):
        messages.append(message)

    middleware = RequestLimitsMiddleware(handler)
    with pytest.raises(TimeoutError, match="after response start"):
        await middleware(scope(method="GET"), receive, send)
    assert messages == [{"type": "http.response.start", "status": 200, "headers": []}]
    assert middleware.active == 0


@pytest.mark.parametrize("protocol", ["codex", "claude"])
async def test_disconnect_while_waiting_for_upstream_headers(settings, protocol):
    entered = asyncio.Event()
    disconnected = asyncio.Event()
    cancelled = False

    async def upstream(_request):
        nonlocal cancelled
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled = True

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        app = Starlette()
        app.state.http_client = client
        path = "/codex/v1/responses" if protocol == "codex" else "/claude/v1/messages"
        route = proxy_codex_responses if protocol == "codex" else proxy_claude_messages

        async def handler(s, receive, send):
            response = await route(Request(s, receive))
            await response(s, receive, send)

        upload = True

        async def receive():
            nonlocal upload
            if upload:
                upload = False
                return {"type": "http.request", "body": b'{"model":"fixture","stream":true}'}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(_message):
            pytest.fail("Disconnected pre-header request must not fabricate a response.")

        middleware = RequestLimitsMiddleware(handler)
        task = asyncio.create_task(middleware({**scope(path), "app": app}, receive, send))
        await asyncio.wait_for(entered.wait(), 1)
        disconnected.set()
        await asyncio.wait_for(task, 1)
        assert cancelled and middleware.active == 0


def test_claude_replaces_all_incoming_encoding_preferences():
    headers = build_claude_outbound_headers(
        {"Accept-Encoding": "br", "accept-encoding": "deflate", "ACCEPT-ENCODING": "gzip, deflate"},
        bearer_token="fixture", integration_id="fixture", editor_version="fixture",
    )
    request = httpx.Request("POST", "https://upstream.test", headers=headers)
    assert request.headers.get_list("accept-encoding") == ["gzip, identity"]


@pytest.mark.parametrize("protocol", ["codex", "claude"])
@pytest.mark.parametrize("catalog", [False, True])
def test_actual_routes_negotiate_only_supported_encodings(protocol, catalog):
    captured = []

    def upstream(request):
        captured.append(request.headers.get_list("accept-encoding"))
        body = b'{"data":[]}' if catalog else b'{"ok":true}'
        compressed = gzip.compress(body)
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Chunks([
            compressed[i:i + 1] for i in range(len(compressed))
        ]))

    with TestClient(make_app(upstream)) as client:
        if catalog:
            response = client.get(f"/{protocol}/v1/models", headers={"Accept-Encoding": "br, deflate"})
        else:
            path = "/codex/v1/responses" if protocol == "codex" else "/claude/v1/messages"
            response = client.post(path, json={"model": "fixture"}, headers={"Accept-Encoding": "br, deflate"})
    assert response.status_code == 200
    assert all(value == ["gzip, identity"] for value in captured)
    assert captured


@pytest.mark.parametrize("protocol", ["codex", "claude"])
@pytest.mark.parametrize("encoding", ["deflate", "gzip, deflate"])
def test_unnegotiated_raw_deflate_and_chains_fail_as_protocol_errors(protocol, encoding):
    compressor = zlib.compressobj(wbits=-15)
    raw = compressor.compress(b'{"ok":true}') + compressor.flush()
    with TestClient(make_app(lambda request: httpx.Response(
        200, headers={"content-encoding": encoding}, stream=Chunks([raw]),
    ))) as client:
        path = "/codex/v1/responses" if protocol == "codex" else "/claude/v1/messages"
        response = client.post(path, json={"model": "fixture"})
    assert response.status_code == 502
    assert "error" in response.json()


class FailedClose(Chunks):
    async def aclose(self):
        self.closed = True
        await asyncio.sleep(0.001)
        raise httpx.ReadError("Authorization: Bearer synthetic-close-secret-12345")


@pytest.mark.parametrize("protocol", ["codex", "claude"])
@pytest.mark.parametrize("catalog", [False, True])
@pytest.mark.parametrize("status", [200, 401, 403, 429])
@pytest.mark.parametrize("oversized", [False, True])
def test_cleanup_failure_is_structured_and_retains_known_status(protocol, catalog, status, oversized, caplog):
    # Oversized error bodies stop before EOF; success EOF failures exercise the
    # automatic close path. Neither may escape as Starlette's plain-text 500.
    body = b"x" * 40000 if oversized else b'{"data":[]}'
    stream = FailedClose([body])
    with TestClient(make_app(lambda _request: httpx.Response(
        status, headers={"retry-after": "3"}, stream=stream,
    )), raise_server_exceptions=False) as client:
        if catalog:
            response = client.get(f"/{protocol}/v1/models")
        else:
            path = "/codex/v1/responses" if protocol == "codex" else "/claude/v1/messages"
            response = client.post(path, json={"model": "fixture"})
    assert response.status_code == (502 if status == 200 else status)
    assert "error" in response.json()
    if status != 200:
        assert response.headers["retry-after"] == "3"
    assert stream.closed
    assert "synthetic-close-secret" not in response.text + caplog.text


@pytest.mark.parametrize("protocol", ["codex", "claude"])
def test_cleanup_failure_does_not_hide_size_limit(settings, protocol):
    reset_settings_for_tests(replace(settings, max_response_bytes=4))
    stream = FailedClose([b"oversized"])
    with TestClient(make_app(lambda _request: httpx.Response(200, stream=stream))) as client:
        path = "/codex/v1/responses" if protocol == "codex" else "/claude/v1/messages"
        response = client.post(path, json={"model": "fixture"})
    assert response.status_code == 502
    assert "size limit" in response.json()["error"]["message"]
    assert stream.closed


async def test_cleanup_failure_still_propagates_caller_cancellation(caplog):
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowFailure(FailedClose):
        async def aclose(self):
            entered.set()
            await release.wait()
            await super().aclose()

    stream = SlowFailure([])
    response = httpx.Response(200, stream=stream)
    protect_response(response)
    task = asyncio.create_task(close_response(response))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
    assert "synthetic-close-secret" not in caplog.text
