"""Tests for Claude handlers, shared streaming helpers, model filtering, and health."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import copilot_cli_relay.claude_proxy as claude_proxy_mod
import copilot_cli_relay.proxy_shared as proxy_shared_mod
from copilot_cli_relay.claude_proxy import (
    _anthropic_kind_for_status,
    _normalize_effort,
    _parse_claude_request,
    claude_healthz,
    proxy_claude_messages,
    proxy_claude_models,
)
from copilot_cli_relay.config import Settings, reset_settings_for_tests
from copilot_cli_relay.proxy_shared import passthrough_response


@pytest.fixture(autouse=True)
def _settings():
    s = Settings(
        proxy_port=4141,
        github_token="gho_test",
        api_base="https://upstream.test",
        integration_id="copilot-developer-cli",
        editor_version="copilot-cli-relay/test",
        log_level="info",
        log_bodies=True,  # exercise body-logging code path
    )
    reset_settings_for_tests(s)
    yield s
    reset_settings_for_tests(None)


def _make_client(handler):
    """Build app with proxy routes and inject httpx.AsyncClient using MockTransport."""
    app = Starlette(
        routes=[
            Route("/claude/v1/messages", proxy_claude_messages, methods=["POST"]),
            Route("/claude/v1/models", proxy_claude_models, methods=["GET"]),
            Route("/claude/healthz", claude_healthz, methods=["GET"]),
            Route("/healthz", claude_healthz, methods=["GET"]),
        ],
    )
    transport = httpx.MockTransport(handler)
    app.state.http_client = httpx.AsyncClient(transport=transport)
    return TestClient(app), app


def test_parse_claude_request_stream_strict_boolean():
    """Per Anthropic spec `stream` is a JSON boolean. Truthy non-bool values
    (string "false", int 1, dict {}) must NOT be treated as streaming —
    otherwise the proxy returns the wrong content framing."""
    # True bool → streaming
    _b, _m, s = _parse_claude_request(b'{"stream": true}')
    assert s is True
    # Anything else → not streaming, even if Python-truthy
    for raw in (
        b'{"stream": false}',
        b'{"stream": null}',
        b'{"stream": "true"}',
        b'{"stream": "false"}',
        b'{"stream": 1}',
        b'{"stream": 0}',
        b'{"stream": []}',
        b'{"stream": {}}',
        b'{}',
    ):
        _b, _m, s = _parse_claude_request(raw)
        assert s is False, f"expected non-streaming for {raw!r}"


# ---------- helper-function unit tests ----------

@pytest.mark.parametrize(
    "status,kind",
    [
        (401, "authentication_error"),
        (403, "permission_error"),
        (404, "not_found_error"),
        (429, "rate_limit_error"),
        (500, "api_error"),
        (502, "api_error"),
        (599, "api_error"),
        (400, "invalid_request_error"),
        (418, "invalid_request_error"),
    ],
)
def test_anthropic_kind_for_status(status, kind):
    assert _anthropic_kind_for_status(status) == kind


def test_parse_claude_request_stream_and_model_extraction():
    """_parse_claude_request replaces the old _wants_stream/_model_from_body helpers;
    confirm the same semantics for streaming flag + model extraction + malformed."""
    body, model, stream = _parse_claude_request(b'{"stream": true, "model": "claude-x"}')
    assert stream is True and model == "claude-x"
    body, model, stream = _parse_claude_request(b'{"stream": false, "model": "claude-x"}')
    assert stream is False and model == "claude-x"
    body, model, stream = _parse_claude_request(b'{}')
    assert stream is False and model is None
    body, model, stream = _parse_claude_request(b'not json')
    assert stream is False and model is None
    assert body == b'not json'  # malformed bodies forwarded unchanged


def test_parse_claude_request_malformed_body_returns_defaults():
    body, model, streaming = claude_proxy_mod._parse_claude_request(b"not json{{{")
    assert body == b"not json{{{"
    assert model is None
    assert streaming is False


def test_parse_claude_request_non_dict_body_returns_defaults():
    body, model, streaming = claude_proxy_mod._parse_claude_request(b'["a","b"]')
    assert body == b'["a","b"]'
    assert model is None
    assert streaming is False


def test_parse_claude_request_dict_body_returns_metadata():
    body, model, streaming = claude_proxy_mod._parse_claude_request(b'{"model":"m","stream":true}')
    assert model == "m"
    assert streaming is True
    # body is re-serialized but semantically equal.
    assert json.loads(body) == {"model": "m", "stream": True}


def test_normalize_effort_alias_and_passthrough():
    allowed = {"low", "medium", "high"}
    assert _normalize_effort("xhigh", allowed) == "high"
    assert _normalize_effort("MINIMAL", allowed) == "low"
    assert _normalize_effort("medium", allowed) == "medium"
    # Non-string falls back to medium when present.
    assert _normalize_effort(7, allowed) == "medium"
    # Single-value allowed set with non-string -> that single value.
    assert _normalize_effort(None, {"medium"}) == "medium"
    # Unknown string when medium not allowed -> falls back to allowed value.
    assert _normalize_effort("bogus", {"low"}) == "low"
    # Unknown string when medium IS allowed -> medium.
    assert _normalize_effort("bogus", allowed) == "medium"


def test_rewrite_body_drops_non_dict_root():
    raw = b'["not","an","object"]'
    out, model, stream = claude_proxy_mod._parse_claude_request(raw)
    assert out == raw
    assert model is None
    assert stream is False


def test_rewrite_body_strips_empty_output_config_after_haiku_strip():
    raw = json.dumps({
        "model": "claude-haiku-4.5",
        "output_config": {"effort": "high"},
    }).encode()
    out, _model, _stream = claude_proxy_mod._parse_claude_request(raw)
    out_obj = json.loads(out)
    assert "output_config" not in out_obj


def test_rewrite_body_keeps_non_empty_output_config():
    raw = json.dumps({
        "model": "claude-haiku-4.5",
        "output_config": {"effort": "high", "other": 1},
    }).encode()
    out, _model, _stream = claude_proxy_mod._parse_claude_request(raw)
    out_obj = json.loads(out)
    # effort field stripped, but other remains so output_config stays.
    assert out_obj["output_config"] == {"other": 1}


def test_passthrough_response_drops_hop_by_hop():
    upstream = httpx.Response(
        201,
        content=b"hello",
        headers={
            "transfer-encoding": "chunked",
            "content-length": "5",
            "connection": "keep-alive",
            "x-keep": "yes",
            "content-type": "text/plain",
        },
    )
    # Inject content-encoding manually after construction so httpx doesn't try
    # to gzip-decode the literal bytes; we just want to verify it's stripped.
    upstream.headers["content-encoding"] = "gzip"
    r = passthrough_response(upstream)
    assert r.status_code == 201
    assert r.body == b"hello"
    keys = {k.lower() for k in r.headers}
    for h in ("transfer-encoding", "content-encoding", "connection"):
        assert h not in keys
    assert r.headers["x-keep"] == "yes"
    assert r.headers["content-type"] == "text/plain"


def test_passthrough_response_strips_trailer_singular():
    """RFC 7230 §6.1 lists the hop-by-hop header as `Trailer` (singular)."""
    upstream = httpx.Response(200, content=b"x", headers={"trailer": "expires", "x-keep": "1"})
    r = passthrough_response(upstream)
    keys = {k.lower() for k in r.headers}
    assert "trailer" not in keys
    assert r.headers["x-keep"] == "1"


def test_passthrough_response_strips_connection_named_dynamic_hop_by_hop():
    """RFC 7230 §6.1: response-side header names listed in `Connection` are
    also per-hop and must not be forwarded to the client."""
    upstream = httpx.Response(
        200,
        content=b"x",
        headers={
            "connection": "close, X-Server-Hop",
            "x-server-hop": "must-not-leak",
            "x-keep": "1",
        },
    )
    r = passthrough_response(upstream)
    keys = {k.lower() for k in r.headers}
    assert "x-server-hop" not in keys
    assert "connection" not in keys
    assert r.headers["x-keep"] == "1"


def test_messages_streaming_success_forwards_upstream_headers():
    """Regression: streaming success path must forward upstream response
    headers (request-id correlation, vendor rate-limit hints, etc.) the
    same way non-streaming does — minus hop-by-hop and our framing
    overrides."""
    chunks = [b"event: message_start\ndata: {\"type\":\"message_start\"}\n\n"]

    def handler(request):
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b"".join(chunks)),
            headers={
                "content-type": "text/event-stream",
                "x-request-id": "upstream-rid-12345",
                "x-ratelimit-remaining": "42",
                "connection": "close, X-Server-Hop",
                "x-server-hop": "must-not-leak",
            },
        )

    client, _ = _make_client(handler)
    with client.stream("POST", "/claude/v1/messages", json={"model": "x", "stream": True}) as r:
        assert r.status_code == 200
        # Forwarded
        assert r.headers.get("x-request-id") == "upstream-rid-12345"
        assert r.headers.get("x-ratelimit-remaining") == "42"
        # Stripped: hop-by-hop static + Connection-named dynamic
        keys = {k.lower() for k in r.headers}
        assert "connection" not in keys
        assert "x-server-hop" not in keys
        # Our framing overrides
        assert r.headers["cache-control"] == "no-cache"
        assert r.headers["x-accel-buffering"] == "no"
        # Drain so the underlying stream closes cleanly.
        b"".join(r.iter_bytes())


def test_parse_claude_request_no_rewrite_returns_original_bytes():
    """When no effort fields need rewriting, _parse_claude_request must return the
    exact original byte sequence (not a re-serialized copy). Avoids
    ensure_ascii expansion of unicode and preserves byte-for-byte shape."""
    # Whitespace + unicode + key ordering that json.dumps would reshape.
    raw = b'{ "model" : "claude-sonnet-4-6",  "messages": [{"content": "h\xc3\xa9llo \xf0\x9f\x98\x80"}] }'
    out, model, _stream = claude_proxy_mod._parse_claude_request(raw)
    assert out is raw  # identity, not just equality
    assert model == "claude-sonnet-4-6"


def test_parse_claude_request_rewrite_keeps_unicode_unescaped():
    """When a rewrite IS required, the re-serialized body must keep unicode
    chars as UTF-8 bytes (ensure_ascii=False), not \\uXXXX escapes."""
    raw = (
        b'{"model":"claude-haiku-4.5","reasoning_effort":"high",'
        b'"messages":[{"content":"caf\xc3\xa9"}]}'
    )
    out, _model, _stream = claude_proxy_mod._parse_claude_request(raw)
    assert out is not raw  # rewrite happened
    assert b"\xc3\xa9" in out  # raw UTF-8 bytes for é preserved
    assert b"\\u00e9" not in out
    assert b"reasoning_effort" not in out  # haiku strips the field


def test_messages_inbound_authorization_never_reaches_upstream():
    """Regression: a leaked Authorization or x-api-key header from the inbound
    request must never be forwarded to the upstream Copilot API."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ok": True})

    client, _ = _make_client(handler)
    r = client.post(
        "/claude/v1/messages",
        json={"model": "claude-sonnet-4-6"},
        headers={
            "Authorization": "Bearer LEAKED-CLIENT-TOKEN",
            "x-api-key": "sk-leaked",
            "proxy-authorization": "Basic dXNlcjpwYXNz",
        },
    )
    assert r.status_code == 200
    # Upstream sees ONLY our Bearer token.
    assert captured["headers"]["authorization"] == "Bearer gho_test"
    assert "LEAKED-CLIENT-TOKEN" not in str(captured["headers"])
    assert "x-api-key" not in {k.lower() for k in captured["headers"]}
    assert "proxy-authorization" not in {k.lower() for k in captured["headers"]}


# ---------- /v1/messages non-streaming ----------

def test_messages_non_streaming_success():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={"id": "msg_1", "type": "message"},
            headers={"x-upstream": "ok"},
        )

    client, _ = _make_client(handler)
    payload = {"model": "claude-sonnet-4.6", "messages": [{"role": "user", "content": "hi"}]}
    r = client.post("/claude/v1/messages", json=payload)
    assert r.status_code == 200
    assert r.json()["id"] == "msg_1"
    assert r.headers["x-upstream"] == "ok"
    # Auth header swapped to bearer token.
    assert captured["headers"]["authorization"] == "Bearer gho_test"
    assert captured["headers"]["copilot-integration-id"] == "copilot-developer-cli"
    assert captured["url"] == "https://upstream.test/v1/messages"


def test_messages_body_rewrite_applied_before_send():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    client, _ = _make_client(handler)
    r = client.post(
        "/claude/v1/messages",
        json={"model": "claude-opus-4.7", "reasoning_effort": "xhigh"},
    )
    assert r.status_code == 200
    assert captured["body"]["reasoning_effort"] == "medium"


def test_messages_non_streaming_error_body_read_failure_still_returns_status(caplog):
    """Regression: if the upstream error-body stream itself raises during read
    (httpx.ReadError, OSError, etc.), the proxy must still return a JSON
    envelope with the upstream HTTP status — same guarantee as the streaming
    path."""
    class FlakeyErrorStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial body Authorization: Bearer leaked-non-stream-err-token-xyz"
            raise httpx.ReadError("non-stream err body read failed Authorization: Bearer leaked-during-non-stream-err-2222")

        async def aclose(self):
            pass

    def handler(request):
        return httpx.Response(503, stream=FlakeyErrorStream(), headers={"content-type": "text/plain"})

    client, _ = _make_client(handler)
    with caplog.at_level("WARNING"):
        r = client.post("/claude/v1/messages", json={"model": "x"})
    assert r.status_code == 503
    body = r.json()
    assert body["error"]["type"] == "api_error"
    msg = body["error"]["message"]
    # Whatever partial bytes we read must be redacted, and the inner read
    # failure's exception text must also be redacted in logs.
    assert "leaked-non-stream-err-token-xyz" not in msg
    log_text = "\n".join(rec.message for rec in caplog.records)
    assert "leaked-non-stream-err-token-xyz" not in log_text
    assert "leaked-during-non-stream-err-2222" not in log_text


def test_messages_non_streaming_error_body_redacted_when_log_bodies(caplog):
    """Regression: non-streaming upstream error bodies must be redacted +
    bounded just like streaming ones. A misconfigured/hostile upstream that
    reflects request headers verbatim could otherwise leak the proxy's
    Bearer token into the local client's response body."""
    def handler(request):
        return httpx.Response(
            500,
            content=b'{"err": "Authorization: Bearer leaked-non-stream-9999"}',
        )

    client, _ = _make_client(handler)
    with caplog.at_level("WARNING"):
        r = client.post("/claude/v1/messages", json={"model": "x"})
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["type"] == "api_error"
    msg = body["error"]["message"]
    assert "leaked-non-stream-9999" not in msg
    assert "***REDACTED***" in msg
    log_text = "\n".join(rec.message for rec in caplog.records)
    assert "leaked-non-stream-9999" not in log_text


def test_messages_non_streaming_error_body_suppressed_when_log_bodies_off(caplog):
    s = Settings(
        proxy_port=4141, github_token="gho_test",
        api_base="https://upstream.test",
        integration_id="copilot-developer-cli",
        editor_version="copilot-cli-relay/test",
        log_level="info", log_bodies=False,
    )
    reset_settings_for_tests(s)
    try:
        def handler(request):
            return httpx.Response(429, content=b"sensitive non-stream details")

        client, _ = _make_client(handler)
        with caplog.at_level("WARNING"):
            r = client.post("/claude/v1/messages", json={"model": "x"})
        assert r.status_code == 429
        log_text = "\n".join(rec.message for rec in caplog.records)
        assert "sensitive non-stream details" not in log_text
        assert "body suppressed" in log_text
    finally:
        reset_settings_for_tests(None)


def test_messages_non_streaming_error_forwards_retry_after():
    """SDK retry on 429/401 keys on Retry-After / WWW-Authenticate /
    X-RateLimit-* — same forwarding logic as the streaming pre-error path."""
    def handler(request):
        return httpx.Response(
            429,
            content=b"slow down",
            headers={
                "retry-after": "30",
                "x-ratelimit-remaining": "0",
                "www-authenticate": "Bearer realm=\"copilot\"",
                "content-type": "application/json",
            },
        )

    client, _ = _make_client(handler)
    r = client.post("/claude/v1/messages", json={"model": "x"})
    assert r.status_code == 429
    assert r.headers.get("retry-after") == "30"
    assert r.headers.get("x-ratelimit-remaining") == "0"
    assert "Bearer" in r.headers.get("www-authenticate", "")


def test_messages_non_streaming_error_status_kind_mapping():
    """Status code maps correctly to Anthropic error type."""
    def handler(request):
        return httpx.Response(403, content=b"forbidden")

    client, _ = _make_client(handler)
    r = client.post("/claude/v1/messages", json={"model": "x"})
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "permission_error"


def test_passthrough_response_strips_server_header():
    """Hygiene parity with Set-Cookie: drop upstream `Server` advertisement."""
    upstream = httpx.Response(
        200,
        content=b"x",
        headers={"server": "nginx/1.25.3 (Ubuntu)", "x-keep": "1"},
    )
    r = passthrough_response(upstream)
    keys = {k.lower() for k in r.headers}
    assert "server" not in keys
    assert r.headers["x-keep"] == "1"


# ---------- 1M context routing ----------


def test_1m_remap_opus_47_with_beta_routes_to_internal_variant():
    """Claude Code's 'Opus 4.7 (1M context)' picker tier sends model
    `claude-opus-4-7` plus `anthropic-beta: context-1m-2025-08-07`. Without
    a remap the beta gets stripped (Copilot rejects it) and the request
    silently downgrades to the 200K-context model. We rewrite the model id
    to the upstream variant whose advertised context window is actually 1M,
    converting from Claude Code's dash form to the dot form Copilot's
    /v1/messages requires for these specific ids."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ok": True})

    client, _ = _make_client(handler)
    r = client.post(
        "/claude/v1/messages",
        json={"model": "claude-opus-4-7", "messages": []},
        headers={"anthropic-beta": "context-1m-2025-08-07"},
    )
    assert r.status_code == 200
    # Upstream sees dot form (only form Copilot accepts for -1m ids).
    assert captured["body"]["model"] == "claude-opus-4.7-1m-internal"
    # The beta itself is still stripped — upstream rejects it on -1m models too.
    assert "anthropic-beta" not in {k.lower() for k in captured["headers"]}


def test_1m_remap_opus_46_with_beta_routes_to_1m_variant():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client, _ = _make_client(handler)
    client.post(
        "/claude/v1/messages",
        json={"model": "claude-opus-4.6", "messages": []},
        headers={"anthropic-beta": "context-1m-2025-08-07"},
    )
    assert captured["body"]["model"] == "claude-opus-4.6-1m"


def test_1m_remap_dot_form_model_id_also_remapped():
    """Claude Code may send dot or dash forms — both must remap to the
    upstream-accepted dot form (-1m ids reject dash upstream)."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client, _ = _make_client(handler)
    client.post(
        "/claude/v1/messages",
        json={"model": "Claude-Opus-4.7", "messages": []},  # mixed case + dots
        headers={"anthropic-beta": "context-1m-2025-08-07, tools-2024-04-04"},
    )
    assert captured["body"]["model"] == "claude-opus-4.7-1m-internal"


def test_1m_remap_mixed_case_beta_token_remaps_AND_strips():
    """Regression: detection (claude_proxy.py) and stripping (headers.py) of the
    1M beta token must agree on case-insensitivity. A mixed-case
    `Context-1M-2025-08-07` should both trigger the model remap AND get
    stripped from the outbound headers — otherwise upstream rejects it."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ok": True})

    client, _ = _make_client(handler)
    r = client.post(
        "/claude/v1/messages",
        json={"model": "claude-opus-4-7", "messages": []},
        headers={"anthropic-beta": "Context-1M-2025-08-07"},
    )
    assert r.status_code == 200
    # Model was remapped (detection side) and dash→dot normalized for upstream.
    assert captured["body"]["model"] == "claude-opus-4.7-1m-internal"
    # Beta was stripped from outbound (sending side)
    assert "anthropic-beta" not in {k.lower() for k in captured["headers"]}


def test_1m_remap_no_beta_means_no_remap():
    """Without the 1M beta header, the model id passes through untouched."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client, _ = _make_client(handler)
    client.post("/claude/v1/messages", json={"model": "claude-opus-4-7", "messages": []})
    assert captured["body"]["model"] == "claude-opus-4-7"


def test_1m_remap_unknown_model_no_op():
    """Sonnet has no -1m variant on this tenant — leave the model unchanged
    so the request still reaches upstream (downgraded to 200K, but at least
    doesn't 404 on a fictional id)."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client, _ = _make_client(handler)
    client.post(
        "/claude/v1/messages",
        json={"model": "claude-sonnet-4-6", "messages": []},
        headers={"anthropic-beta": "context-1m-2025-08-07"},
    )
    assert captured["body"]["model"] == "claude-sonnet-4-6"


def test_1m_remap_user_already_picked_1m_id_directly():
    """User typed `/model claude-opus-4-7-1m-internal` (dash, what
    /v1/models advertises and what Claude Code's /model validates against).
    No beta header, no remap needed — but the dash→dot upstream conversion
    MUST still fire so Copilot doesn't reject with 'model_not_supported'."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client, _ = _make_client(handler)
    client.post(
        "/claude/v1/messages",
        json={"model": "claude-opus-4-7-1m-internal", "messages": []},
    )
    # Dash in (from Claude Code), dot out (what Copilot accepts).
    assert captured["body"]["model"] == "claude-opus-4.7-1m-internal"


def test_1m_dash_to_dot_normalization_for_opus_46_1m():
    """Same dash→dot guarantee for the Opus 4.6 -1m variant."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client, _ = _make_client(handler)
    client.post("/claude/v1/messages", json={"model": "claude-opus-4-6-1m", "messages": []})
    assert captured["body"]["model"] == "claude-opus-4.6-1m"


def test_no_dash_to_dot_normalization_for_non_1m_models():
    """Dash form is preserved for non-1m models — upstream accepts both
    dash and dot for those, no need to mutate."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client, _ = _make_client(handler)
    client.post("/claude/v1/messages", json={"model": "claude-sonnet-4-6", "messages": []})
    assert captured["body"]["model"] == "claude-sonnet-4-6"  # unchanged


def test_dash_to_dot_normalization_handles_future_1m_variants():
    """Future-proof: a hypothetical `claude-sonnet-4-6-1m` (no -internal) and
    `claude-haiku-4-5-1m-internal` should be auto-converted to dot form
    upstream by the generic regex, without needing a table update."""
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"ok": True})

    client, _ = _make_client(handler)
    for dashed in ("claude-sonnet-4-6-1m", "claude-haiku-4-5-1m-internal"):
        client.post("/claude/v1/messages", json={"model": dashed, "messages": []})
    assert captured == ["claude-sonnet-4.6-1m", "claude-haiku-4.5-1m-internal"]


def test_to_upstream_dot_form_unit():
    """Direct unit coverage of the version-segment dash→dot helper."""
    f = claude_proxy_mod._to_upstream_dot_form
    assert f("claude-opus-4-7-1m-internal") == "claude-opus-4.7-1m-internal"
    assert f("claude-opus-4-6-1m") == "claude-opus-4.6-1m"
    # Dot-form input is rejected by the regex (it requires dash-form version
    # segments). _normalize_model_for_upstream's `target is None` branch then
    # treats it as a no-op, which is correct: dot form is what upstream wants.
    assert f("claude-opus-4.7-1m-internal") is None
    # Non-1m ids: not recognized.
    assert f("claude-opus-4-7") is None
    assert f("claude-sonnet-4-6") is None
    # Single-digit families still work.
    assert f("claude-opus-10-2-1m") == "claude-opus-10.2-1m"


def test_normalize_model_for_upstream_defensive_paths():
    """Direct unit coverage of _normalize_model_for_upstream's safety guards
    for non-string inputs and malformed JSON bodies."""
    f = claude_proxy_mod._normalize_model_for_upstream
    # Non-string model_id → no-op
    out, m, mut = f(b'{"model": null}', None)
    assert (out, m, mut) == (b'{"model": null}', None, False)
    # Recognized -1m id but malformed JSON body → no-op (defensive)
    out, m, mut = f(b"not json{{", "claude-opus-4-7-1m-internal")
    assert mut is False and m == "claude-opus-4-7-1m-internal"


def test_remap_to_1m_defensive_paths():
    """Direct unit coverage of _remap_to_1m's safety guards: non-string
    model_id, malformed JSON body, and non-dict JSON root all return the
    inputs unchanged with mutated=False."""
    # Non-string model_id (e.g. None upstream of a malformed inbound).
    out, m, mut = claude_proxy_mod._remap_to_1m(b'{"model": null}', None)
    assert (out, m, mut) == (b'{"model": null}', None, False)
    # Malformed JSON body (shouldn't happen post _parse_claude_request, but defended).
    out, m, mut = claude_proxy_mod._remap_to_1m(b"not json{{", "claude-opus-4-7")
    assert mut is False and m == "claude-opus-4-7"
    # JSON root that isn't a dict.
    out, m, mut = claude_proxy_mod._remap_to_1m(b'["arr"]', "claude-opus-4-7")
    assert mut is False and m == "claude-opus-4-7"


def test_messages_non_streaming_timeout():
    def handler(request):
        raise httpx.ConnectTimeout("timeout!")

    client, _ = _make_client(handler)
    r = client.post("/claude/v1/messages", json={"model": "x"})
    assert r.status_code == 502
    body = r.json()
    assert body["error"]["type"] == "api_error"
    assert "timeout" in body["error"]["message"].lower()


def test_messages_non_streaming_http_error():
    def handler(request):
        raise httpx.ConnectError("boom")

    client, _ = _make_client(handler)
    r = client.post("/claude/v1/messages", json={"model": "x"})
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "api_error"


def test_messages_non_streaming_exception_text_redacted(caplog):
    """Regression: exception messages may contain credentials (e.g. a URL with
    embedded auth) that must be redacted before reaching the client or logs."""
    def handler(request):
        raise httpx.ConnectError("Authorization: Bearer leaked-exc-token-12345 failed")

    client, _ = _make_client(handler)
    with caplog.at_level("WARNING"):
        r = client.post("/claude/v1/messages", json={"model": "x"})
    assert r.status_code == 502
    msg = r.json()["error"]["message"]
    assert "leaked-exc-token-12345" not in msg
    assert "***REDACTED***" in msg
    log_text = "\n".join(rec.message for rec in caplog.records)
    assert "leaked-exc-token-12345" not in log_text


def test_messages_non_streaming_timeout_text_redacted(caplog):
    def handler(request):
        raise httpx.ReadTimeout("Authorization: Bearer leaked-timeout-token-77777")

    client, _ = _make_client(handler)
    with caplog.at_level("WARNING"):
        r = client.post("/claude/v1/messages", json={"model": "x"})
    assert r.status_code == 502
    msg = r.json()["error"]["message"]
    assert "leaked-timeout-token-77777" not in msg
    log_text = "\n".join(rec.message for rec in caplog.records)
    assert "leaked-timeout-token-77777" not in log_text


def test_messages_non_streaming_success_read_error_is_anthropic_json(caplog):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"partial":'
            raise httpx.ReadError("Authorization: Bearer leaked-success-read-token-12345")

        async def aclose(self):
            pass

    def handler(request):
        return httpx.Response(200, stream=BrokenStream())

    client, _ = _make_client(handler)
    with caplog.at_level("WARNING"):
        r = client.post("/claude/v1/messages", json={"model": "x"})
    assert r.status_code == 502
    assert r.headers["content-type"].startswith("application/json")
    msg = r.json()["error"]["message"]
    assert "Upstream error" in msg
    assert "leaked-success-read-token-12345" not in msg
    log_text = "\n".join(rec.message for rec in caplog.records)
    assert "leaked-success-read-token-12345" not in log_text


def test_messages_non_streaming_success_read_timeout_is_anthropic_json(caplog):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"partial":'
            raise httpx.ReadTimeout("Authorization: Bearer leaked-success-timeout-token-12345")

        async def aclose(self):
            pass

    def handler(request):
        return httpx.Response(200, stream=BrokenStream())

    client, _ = _make_client(handler)
    with caplog.at_level("WARNING"):
        r = client.post("/claude/v1/messages", json={"model": "x"})
    assert r.status_code == 502
    assert r.headers["content-type"].startswith("application/json")
    msg = r.json()["error"]["message"]
    assert "Upstream timeout" in msg
    assert "leaked-success-timeout-token-12345" not in msg
    log_text = "\n".join(rec.message for rec in caplog.records)
    assert "leaked-success-timeout-token-12345" not in log_text


def test_messages_non_streaming_success_read_non_http_error_is_anthropic_json(caplog):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"partial":'
            raise OSError("socket failed Authorization: Bearer leaked-success-oserror-token-12345")

        async def aclose(self):
            pass

    def handler(request):
        return httpx.Response(200, stream=BrokenStream())

    client, _ = _make_client(handler)
    with caplog.at_level("WARNING"):
        r = client.post("/claude/v1/messages", json={"model": "x"})
    assert r.status_code == 502
    assert r.headers["content-type"].startswith("application/json")
    msg = r.json()["error"]["message"]
    assert "Upstream error" in msg
    assert "leaked-success-oserror-token-12345" not in msg
    log_text = "\n".join(rec.message for rec in caplog.records)
    assert "leaked-success-oserror-token-12345" not in log_text


def test_models_upstream_exception_text_redacted():
    def handler(request):
        raise httpx.ConnectError("Authorization: Bearer leaked-models-conn-token-9999")

    client, _ = _make_client(handler)
    r = client.get("/claude/v1/models")
    assert r.status_code == 502
    msg = r.json()["error"]["message"]
    assert "leaked-models-conn-token-9999" not in msg
    assert "***REDACTED***" in msg


# ---------- /v1/messages streaming ----------

def test_messages_streaming_success():
    chunks = [
        b"event: message_start\ndata: {\"type\":\"message_start\"}\n\n",
        b"event: content_block_delta\ndata: {\"type\":\"content_block_delta\"}\n\n",
        b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
    ]

    def handler(request):
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b"".join(chunks)),
            headers={"content-type": "text/event-stream"},
        )

    client, _ = _make_client(handler)
    with client.stream("POST", "/claude/v1/messages", json={"model": "x", "stream": True}) as r:
        assert r.status_code == 200
        body = b"".join(r.iter_bytes())
    for c in chunks:
        assert c in body


def test_messages_streaming_upstream_error_response():
    """Pre-stream upstream errors return a real HTTP error status with the
    Anthropic JSON envelope, not a 200 SSE error frame — so SDK retry logic
    keys on HTTP status correctly."""
    def handler(request):
        return httpx.Response(429, content=b"slow down")

    client, _ = _make_client(handler)
    r = client.post("/claude/v1/messages", json={"model": "x", "stream": True})
    assert r.status_code == 429
    assert r.json()["error"]["type"] == "rate_limit_error"


def test_messages_streaming_upstream_error_body_logged_and_redacted_when_log_bodies(caplog):
    # Default fixture has log_bodies=True. Upstream returns an error body that
    # contains a credential — the log line must include the body but with the
    # secret redacted, not the raw secret.
    def handler(request):
        return httpx.Response(
            500,
            content=b'{"err": "Authorization: Bearer leaked-token-9999"}',
        )

    client, _ = _make_client(handler)
    with caplog.at_level("WARNING"):
        r = client.post("/claude/v1/messages", json={"model": "x", "stream": True})
    log_text = "\n".join(rec.message for rec in caplog.records)
    assert "leaked-token-9999" not in log_text
    assert "***REDACTED***" in log_text
    assert "body=" in log_text  # body included because log_bodies=True
    # JSON error envelope to client must also be redacted
    assert r.status_code == 500
    msg = r.json()["error"]["message"]
    assert "leaked-token-9999" not in msg
    assert "***REDACTED***" in msg


def test_messages_streaming_upstream_error_body_suppressed_when_log_bodies_off(caplog):
    s = Settings(
        proxy_port=4141, github_token="gho_test",
        api_base="https://upstream.test",
        integration_id="copilot-developer-cli",
        editor_version="copilot-cli-relay/test",
        log_level="info", log_bodies=False,
    )
    reset_settings_for_tests(s)
    try:
        def handler(request):
            return httpx.Response(500, content=b"sensitive upstream details")

        client, _ = _make_client(handler)
        with caplog.at_level("WARNING"):
            client.post("/claude/v1/messages", json={"model": "x", "stream": True})
        log_text = "\n".join(rec.message for rec in caplog.records)
        assert "sensitive upstream details" not in log_text
        assert "body suppressed" in log_text
    finally:
        reset_settings_for_tests(None)


def test_messages_streaming_http_error_during_stream():
    def handler(request):
        raise httpx.ReadError("connection reset")

    client, _ = _make_client(handler)
    r = client.post("/claude/v1/messages", json={"model": "x", "stream": True})
    assert r.status_code == 502
    assert "Upstream stream error" in r.json()["error"]["message"]


def test_messages_streaming_exception_text_redacted(caplog):
    """Regression: stream-error path must redact exception text before logging
    or echoing to client."""
    def handler(request):
        raise httpx.ReadError("Authorization: Bearer leaked-stream-token-55555")

    client, _ = _make_client(handler)
    with caplog.at_level("WARNING"):
        r = client.post("/claude/v1/messages", json={"model": "x", "stream": True})
    body = r.text
    assert "leaked-stream-token-55555" not in body
    log_text = "\n".join(rec.message for rec in caplog.records)
    assert "leaked-stream-token-55555" not in log_text


def test_passthrough_strips_set_cookie():
    """Defensive: don't relay upstream Set-Cookie to the local client."""
    def handler(request):
        return httpx.Response(
            200,
            content=b'{"ok": true}',
            headers={
                "content-type": "application/json",
                "set-cookie": "session=abc; HttpOnly",
            },
        )

    client, _ = _make_client(handler)
    r = client.post("/claude/v1/messages", json={"model": "x"})
    assert r.status_code == 200
    assert "set-cookie" not in {k.lower() for k in r.headers}


# ---------- chunks_with_keepalive ----------

@pytest.mark.asyncio
async def test_chunks_with_keepalive_emits_ping(monkeypatch):
    monkeypatch.setattr(proxy_shared_mod, "PING_INTERVAL_SECS", 0.02)

    class FakeUpstream:
        def aiter_bytes(self):
            async def gen():
                # Sleep long enough that several pings fire before/after.
                await asyncio.sleep(0.2)
                yield b"chunk-1"
            return gen()

    class FakeRequest:
        async def is_disconnected(self):
            return False

    out = []
    async for chunk, sentinel in proxy_shared_mod.chunks_with_keepalive(FakeUpstream(), FakeRequest()):
        out.append((chunk, sentinel))
        if len(out) >= 5:
            break
    # First yielded item must be a ping sentinel (upstream is silent past PING_INTERVAL_SECS).
    assert out[0] == (b"", "ping")


@pytest.mark.asyncio
async def test_chunks_with_keepalive_chunk_arrives_after_ping(monkeypatch):
    """Regression: prior implementation finalized the upstream generator on
    the first ping timeout, silently truncating the stream. The fix decouples
    the upstream pump from the ping timer via a memory channel."""
    monkeypatch.setattr(proxy_shared_mod, "PING_INTERVAL_SECS", 0.02)

    class FakeUpstream:
        def aiter_bytes(self):
            async def gen():
                await asyncio.sleep(0.1)  # several pings worth
                yield b"chunk-1"
                yield b"chunk-2"
            return gen()

    class FakeRequest:
        async def is_disconnected(self):
            return False

    pings = 0
    chunks: list[bytes] = []
    async for chunk, sentinel in proxy_shared_mod.chunks_with_keepalive(FakeUpstream(), FakeRequest()):
        if sentinel == "ping":
            pings += 1
            if pings > 50:
                pytest.fail("ping loop never resolved")
            continue
        if sentinel is None:
            chunks.append(chunk)

    assert pings >= 1, "expected at least one ping during the slow upstream window"
    assert chunks == [b"chunk-1", b"chunk-2"], (
        "chunks must arrive intact after intervening pings"
    )


@pytest.mark.asyncio
async def test_chunks_with_keepalive_disconnect():
    class FakeUpstream:
        def aiter_bytes(self):
            async def gen():
                await asyncio.sleep(10)
                yield b"never"
            return gen()

    class FakeRequest:
        async def is_disconnected(self):
            return True

    out = []
    async for _chunk, sentinel in proxy_shared_mod.chunks_with_keepalive(FakeUpstream(), FakeRequest()):
        out.append(sentinel)
    assert out == ["disconnect"]


@pytest.mark.asyncio
async def test_chunks_with_keepalive_finishes_cleanly():
    class FakeUpstream:
        def aiter_bytes(self):
            async def gen():
                yield b"a"
                yield b"b"
            return gen()

    class FakeRequest:
        async def is_disconnected(self):
            return False

    out = []
    async for chunk, sentinel in proxy_shared_mod.chunks_with_keepalive(FakeUpstream(), FakeRequest()):
        out.append((chunk, sentinel))
    assert out == [(b"a", None), (b"b", None)]


@pytest.mark.asyncio
async def test_chunks_with_keepalive_producer_exception_logged_and_terminates(caplog):
    """If the upstream pump raises a non-cancellation exception, the consumer
    loop must terminate cleanly and the failure must be logged."""

    class FakeUpstream:
        def aiter_bytes(self):
            async def gen():
                yield b"first"
                raise httpx.ReadError("upstream went away")
            return gen()

    class FakeRequest:
        async def is_disconnected(self):
            return False

    out = []
    raised: list[BaseException] = []
    with caplog.at_level("WARNING", logger="copilot_cli_relay"):
        try:
            async for chunk, sentinel in proxy_shared_mod.chunks_with_keepalive(FakeUpstream(), FakeRequest()):
                if sentinel is None:
                    out.append(chunk)
        except httpx.ReadError as exc:
            raised.append(exc)
    assert out == [b"first"]
    assert raised, "producer exception must propagate so caller emits anthropic_sse_error_event"
    assert any("upstream stream pump error" in r.message for r in caplog.records)


# ---------- streaming disconnect path through full handler ----------

def test_messages_streaming_client_disconnect(monkeypatch):
    """When request.is_disconnected becomes True mid-stream, generator stops."""

    async def slow_stream():
        # Yield a chunk, then sleep so the disconnect-poll fires next.
        yield b"event: ping\ndata: {}\n\n"
        await asyncio.sleep(0.5)
        yield b"event: never\ndata: {}\n\n"

    def handler(request):
        return httpx.Response(
            200,
            stream=httpx.AsyncByteStream() if False else None,
            content=b"",
        )

    # Easier to test the streaming function directly via _stream_response.
    async def run():
        from starlette.requests import Request

        # Build a minimal upstream client returning a streaming response.
        async def app_handler(req):
            return httpx.Response(200, content=b"x")

        # Use MockTransport that returns a stream of bytes one chunk then long pause.
        async def mt_handler(request):
            async def aiter():
                yield b"first\n\n"
                await asyncio.sleep(2.0)
                yield b"second\n\n"
            class S(httpx.AsyncByteStream):
                async def __aiter__(self):
                    async for c in aiter():
                        yield c
                async def aclose(self): pass
            return httpx.Response(200, stream=S())

        client = httpx.AsyncClient(transport=httpx.MockTransport(mt_handler))

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/messages",
            "headers": [],
            "query_string": b"",
        }
        disconnected = {"v": False}
        async def receive():
            if not disconnected["v"]:
                disconnected["v"] = True
                return {"type": "http.request", "body": b"{}", "more_body": False}
            return {"type": "http.disconnect"}

        req = Request(scope, receive=receive)

        # Monkeypatch ping interval small so loop iterates fast.
        monkeypatch.setattr(proxy_shared_mod, "PING_INTERVAL_SECS", 0.05)

        resp = await claude_proxy_mod._stream_response(
            client=client, url="https://upstream.test/v1/messages",
            body=b"{}", headers={}, request=req,
            model="m", request_id="rid", started=0.0,
        )
        # Drain. Once disconnect arrives, generator returns early.
        chunks = []
        async for c in resp.body_iterator:
            chunks.append(c)
            if len(chunks) > 50:
                break
        await client.aclose()
        return chunks

    chunks = asyncio.run(run())
    # We should not get the second (post-sleep) chunk because disconnect short-circuits.
    joined = b"".join(chunks)
    assert b"second" not in joined


def test_messages_streaming_emits_ping_when_upstream_silent(monkeypatch):
    """Cover the SSE ping branch in _stream_response when upstream is slow."""
    monkeypatch.setattr(proxy_shared_mod, "PING_INTERVAL_SECS", 0.02)

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.2)
            yield b"event: done\ndata: {}\n\n"

        async def aclose(self):
            pass

    def handler(request):
        return httpx.Response(200, stream=SlowStream(), headers={"content-type": "text/event-stream"})

    client, _ = _make_client(handler)
    with client.stream("POST", "/claude/v1/messages", json={"model": "x", "stream": True}) as r:
        body = b""
        for chunk in r.iter_bytes():
            body += chunk
            if b"event: ping" in body:
                break
    assert b"event: ping" in body
    assert b'"type":"ping"' in body


def test_messages_streaming_non_http_error_yields_anthropic_json_error(caplog):
    """Regression: producer-side errors that aren't httpx.HTTPError subclasses
    (OSError, ssl.SSLError, etc.) must surface as a clean Anthropic JSON error
    envelope instead of a raw 500 from the Starlette default handler."""
    def handler(request):
        raise OSError("disk full Authorization: Bearer leaked-os-token-12345")

    client, _ = _make_client(handler)
    with caplog.at_level("WARNING"):
        r = client.post("/claude/v1/messages", json={"model": "x", "stream": True})
    assert r.status_code == 502
    body = r.text
    assert "leaked-os-token-12345" not in body
    log_text = "\n".join(rec.message for rec in caplog.records)
    assert "leaked-os-token-12345" not in log_text


def test_chunks_with_keepalive_redacts_producer_exception_in_log(caplog):
    """Regression: producer-error log line must redact secret-bearing exception
    text before writing to the log."""
    import asyncio

    class FakeUpstream:
        def aiter_bytes(self):
            async def gen():
                yield b"first"
                raise httpx.ReadError("Authorization: Bearer leaked-pump-token-99999")
            return gen()

    class FakeRequest:
        async def is_disconnected(self):
            return False

    async def drain():
        with caplog.at_level("WARNING", logger="copilot_cli_relay"):
            try:
                async for _chunk, _sentinel in proxy_shared_mod.chunks_with_keepalive(
                    FakeUpstream(), FakeRequest()
                ):
                    pass
            except httpx.ReadError:
                pass

    asyncio.run(drain())
    log_text = "\n".join(rec.message for rec in caplog.records)
    assert "leaked-pump-token-99999" not in log_text
    assert "***REDACTED***" in log_text


def test_messages_streaming_timeout_returns_anthropic_json_error(caplog):
    """ConnectTimeout from upstream while opening the stream returns a
    proper Anthropic JSON envelope (502), not a stuck connection."""
    def handler(request):
        raise httpx.ConnectTimeout("Authorization: Bearer leaked-conn-timeout-token-123")

    client, _ = _make_client(handler)
    with caplog.at_level("WARNING"):
        r = client.post("/claude/v1/messages", json={"model": "x", "stream": True})
    assert r.status_code == 502
    msg = r.json()["error"]["message"]
    assert "Upstream timeout" in msg
    assert "leaked-conn-timeout-token-123" not in msg
    log_text = "\n".join(rec.message for rec in caplog.records)
    assert "leaked-conn-timeout-token-123" not in log_text


def test_messages_streaming_429_forwards_retry_after_and_ratelimit_headers():
    """Regression: pre-stream 429/401 must forward Retry-After /
    WWW-Authenticate / X-RateLimit-* headers so SDK retry/backoff logic
    gets the upstream's hint."""
    def handler(request):
        return httpx.Response(
            429,
            content=b"slow down",
            headers={
                "retry-after": "12",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": "1700000000",
                "content-type": "application/json",
            },
        )

    client, _ = _make_client(handler)
    r = client.post("/claude/v1/messages", json={"model": "x", "stream": True})
    assert r.status_code == 429
    assert r.headers.get("retry-after") == "12"
    assert r.headers.get("x-ratelimit-remaining") == "0"
    assert r.headers.get("x-ratelimit-reset") == "1700000000"


def test_messages_streaming_401_forwards_www_authenticate():
    def handler(request):
        return httpx.Response(
            401,
            content=b"unauthorized",
            headers={"www-authenticate": "Bearer realm=\"copilot\""},
        )

    client, _ = _make_client(handler)
    r = client.post("/claude/v1/messages", json={"model": "x", "stream": True})
    assert r.status_code == 401
    assert "Bearer" in r.headers.get("www-authenticate", "")


def test_messages_streaming_error_body_read_failure_still_returns_status(caplog):
    """Regression: if the upstream error body stream itself raises during read
    (httpx.ReadError, OSError, etc.), the proxy must still return a JSON
    envelope with the upstream HTTP status — not crash with a 500."""
    class FlakeyErrorStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial body Authorization: Bearer leaked-err-read-token-xyz"
            raise httpx.ReadError("error body read failed Authorization: Bearer leaked-during-err-read-2222")

        async def aclose(self):
            pass

    def handler(request):
        return httpx.Response(503, stream=FlakeyErrorStream(), headers={"content-type": "text/plain"})

    client, _ = _make_client(handler)
    with caplog.at_level("WARNING"):
        r = client.post("/claude/v1/messages", json={"model": "x", "stream": True})
    assert r.status_code == 503
    body = r.json()
    assert body["error"]["type"] == "api_error"
    msg = body["error"]["message"]
    # Whatever partial bytes we read must be redacted, and the inner read
    # failure's exception text must also be redacted in logs.
    assert "leaked-err-read-token-xyz" not in msg
    log_text = "\n".join(rec.message for rec in caplog.records)
    assert "leaked-err-read-token-xyz" not in log_text
    assert "leaked-during-err-read-2222" not in log_text


def test_read_bounded_truncates_at_cap():
    """read_bounded returns at most max_bytes even if upstream sends more."""
    big = b"x" * 100_000

    class FakeResp:
        async def aiter_bytes(self):
            yield big
            yield big  # would push beyond cap if not truncated

    out = asyncio.run(proxy_shared_mod.read_bounded(FakeResp(), 32 * 1024))
    assert len(out) == 32 * 1024


def test_messages_streaming_mid_stream_error_yields_sse_error_frame(caplog):
    """A producer exception that occurs AFTER the upstream stream has opened
    successfully (status 200, some bytes already flowing) must surface as a
    terminal `event: error` SSE frame — not a half-truncated stream."""
    class FlakeyStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"event: message_start\ndata: {}\n\n"
            raise httpx.ReadError("Authorization: Bearer leaked-mid-stream-token-77777")

        async def aclose(self):
            pass

    def handler(request):
        return httpx.Response(200, stream=FlakeyStream(), headers={"content-type": "text/event-stream"})

    client, _ = _make_client(handler)
    with caplog.at_level("WARNING"):
        with client.stream("POST", "/claude/v1/messages", json={"model": "x", "stream": True}) as r:
            assert r.status_code == 200  # stream opened cleanly
            body = b"".join(r.iter_bytes()).decode()
    assert "event: message_start" in body
    assert "event: error" in body
    assert "leaked-mid-stream-token-77777" not in body
    log_text = "\n".join(rec.message for rec in caplog.records)
    assert "leaked-mid-stream-token-77777" not in log_text


# ---------- /v1/messages effort rewrite (case-insensitivity) ----------

def test_effort_override_is_case_insensitive():
    """`Claude-Opus-4.7` (mixed case) must still hit the override and clamp `high`→`medium`."""
    body, _model, _stream = claude_proxy_mod._parse_claude_request(
        json.dumps({"model": "Claude-Opus-4.7", "reasoning_effort": "high"}).encode()
    )
    assert json.loads(body)["reasoning_effort"] == "medium"


# ---------- /v1/messages response: multi-valued headers ----------

def test_passthrough_response_preserves_repeated_headers():
    """Vary, Link, WWW-Authenticate etc. may legitimately repeat. The previous
    dict comprehension collapsed duplicates; raw_headers iteration preserves them."""
    upstream = httpx.Response(
        200,
        content=b"x",
        headers=[("Vary", "Accept"), ("Vary", "Origin"), ("X-Keep", "1")],
    )
    r = passthrough_response(upstream)
    vary_values = [v for k, v in r.raw_headers if k.lower() == b"vary"]
    assert vary_values == [b"Accept", b"Origin"]


# ---------- /v1/models dedup (post-canonicalization) ----------

def test_models_upstream_malformed_payload_shapes_dont_crash():
    """Defensive: a malformed-but-valid-JSON `/models` payload from upstream
    must not reach Starlette's plain-text 500 handler. Each item with a
    wrong-shape field should be silently skipped, not raise AttributeError."""
    models = [
        None,                                                                                 # not a dict
        "not-a-dict-either",                                                                  # not a dict
        {"vendor": "Anthropic", "capabilities": "chat"},                                      # caps not a dict
        {"vendor": "Anthropic", "capabilities": {"type": "chat"}, "id": 12345},               # id not a string
        {"vendor": "Anthropic", "capabilities": {"type": "chat"}, "id": "claude-name-bad",
         "name": ["unexpectedly", "a", "list"]},                                              # name not a string → display_name falls back to id
        {"vendor": "Anthropic", "capabilities": {"type": "chat"}, "id": "claude-good",
         "name": "Claude Good"},                                                              # the only valid one
    ]
    client, _ = _make_client(lambda req: _models_response(models))
    r = client.get("/claude/v1/models")
    assert r.status_code == 200
    body = r.json()
    ids = [m["id"] for m in body["data"]]
    assert ids == ["claude-name-bad", "claude-good"]
    # display_name falls back to id when upstream's name is non-string.
    name_bad = next(m for m in body["data"] if m["id"] == "claude-name-bad")
    assert name_bad["display_name"] == "claude-name-bad"


def test_models_dedups_after_canonicalization():
    """If upstream returns both dot and dash forms of the same model, the
    canonical id should appear only once in the output."""
    models = [
        {"id": "claude-opus-4.7", "name": "Opus 4.7", "vendor": "Anthropic",
         "capabilities": {"type": "chat"}},
        {"id": "claude-opus-4-7", "name": "Opus 4.7 dup", "vendor": "Anthropic",
         "capabilities": {"type": "chat"}},
    ]
    client, _ = _make_client(lambda req: _models_response(models))
    r = client.get("/claude/v1/models")
    ids = [m["id"] for m in r.json()["data"]]
    assert ids == ["claude-opus-4-7"]


def test_models_vendor_filter_case_insensitive():
    """Defensive: upstream returning lowercase 'anthropic' must not yield empty list."""
    models = [
        {"id": "claude-x", "name": "X", "vendor": "anthropic",
         "capabilities": {"type": "chat"}},
    ]
    client, _ = _make_client(lambda req: _models_response(models))
    r = client.get("/claude/v1/models")
    assert [m["id"] for m in r.json()["data"]] == ["claude-x"]




def _models_response(models):
    return httpx.Response(200, json={"data": models})


def test_models_filters_and_canonicalizes():
    models = [
        # Kept and canonicalized.
        {"id": "claude-sonnet-4.6", "name": "Claude Sonnet 4.6", "vendor": "Anthropic",
         "capabilities": {"type": "chat"}},
        {"id": "claude-opus-4.7", "name": "Claude Opus 4.7", "vendor": "Anthropic",
         "capabilities": {"type": "chat"}, "model_picker_enabled": True},
        # Wrong vendor.
        {"id": "gpt-5", "name": "GPT 5", "vendor": "OpenAI",
         "capabilities": {"type": "chat"}},
        # Wrong capability type.
        {"id": "claude-embed", "name": "Embed", "vendor": "Anthropic",
         "capabilities": {"type": "embeddings"}},
        # Picker disabled.
        {"id": "claude-sonnet-4.5", "name": "Claude Sonnet 4.5", "vendor": "Anthropic",
         "capabilities": {"type": "chat"}, "model_picker_enabled": False},
        # -1m variant: kept (real 1M context window upstream — hiding it would
        # block the only path to 1M on this tenant).
        {"id": "claude-opus-4.6-1m", "name": "Claude Opus 4.6 (1M context)(Internal only)",
         "vendor": "Anthropic", "capabilities": {"type": "chat"}},
        {"id": "claude-opus-4.7-1m-internal", "name": "Claude Opus 4.7 (1M context)(Internal only)",
         "vendor": "Anthropic", "capabilities": {"type": "chat"}},
        # Internal-only WITHOUT a -1m id: still excluded (these are MS-private
        # experiments without the redeeming 1M-context window).
        {"id": "claude-secret", "name": "Claude Secret (Internal Only)", "vendor": "Anthropic",
         "capabilities": {"type": "chat"}},
        # Duplicate of first — deduped.
        {"id": "claude-sonnet-4.6", "name": "dup", "vendor": "Anthropic",
         "capabilities": {"type": "chat"}},
        # Missing id — skipped.
        {"name": "no id", "vendor": "Anthropic", "capabilities": {"type": "chat"}},
        # Missing capabilities dict — skipped (not a chat model).
        {"id": "claude-no-caps", "name": "x", "vendor": "Anthropic"},
    ]
    client, _ = _make_client(lambda req: _models_response(models))
    r = client.get("/claude/v1/models")
    assert r.status_code == 200
    body = r.json()
    ids = [m["id"] for m in body["data"]]
    assert ids == [
        "claude-sonnet-4-6",
        "claude-opus-4-7",
        "claude-opus-4-6-1m",
        "claude-opus-4-7-1m-internal",
    ]
    assert body["first_id"] == "claude-sonnet-4-6"
    assert body["last_id"] == "claude-opus-4-7-1m-internal"
    assert body["has_more"] is False
    # display_name defaults from name when present.
    assert body["data"][0]["display_name"] == "Claude Sonnet 4.6"


def test_models_empty_result():
    client, _ = _make_client(lambda req: _models_response([
        {"id": "x", "vendor": "OpenAI", "capabilities": {"type": "chat"}},
    ]))
    r = client.get("/claude/v1/models")
    body = r.json()
    assert body["data"] == []
    assert body["first_id"] is None
    assert body["last_id"] is None


def test_models_upstream_non_200():
    client, _ = _make_client(lambda req: httpx.Response(500, content=b"down"))
    r = client.get("/claude/v1/models")
    assert r.status_code == 500
    assert r.json()["error"]["type"] == "api_error"


def test_models_upstream_exception():
    def handler(req):
        raise httpx.ConnectError("nope")
    client, _ = _make_client(handler)
    r = client.get("/claude/v1/models")
    assert r.status_code == 502
    assert "Upstream /models error" in r.json()["error"]["message"]


def test_models_upstream_200_non_json_body():
    """Captive portal / HTML error page on a 200 must not 500 the client."""
    client, _ = _make_client(lambda req: httpx.Response(
        200, content=b"<html>captive portal</html>",
        headers={"content-type": "text/html"},
    ))
    r = client.get("/claude/v1/models")
    assert r.status_code == 502
    body = r.json()
    assert body["error"]["type"] == "api_error"
    assert "non-JSON" in body["error"]["message"]


def test_models_upstream_200_json_array_payload():
    """Defensive: upstream returns a JSON array (not the expected dict)."""
    client, _ = _make_client(lambda req: httpx.Response(200, json=["unexpected", "shape"]))
    r = client.get("/claude/v1/models")
    assert r.status_code == 200
    assert r.json()["data"] == []


def test_models_upstream_error_body_redacted():
    """Upstream /models error must redact the body before echoing it to the client."""
    client, _ = _make_client(lambda req: httpx.Response(
        500, content=b'{"err": "Authorization: Bearer leaked-models-token-9999"}'
    ))
    r = client.get("/claude/v1/models")
    assert r.status_code == 500
    msg = r.json()["error"]["message"]
    assert "leaked-models-token-9999" not in msg
    assert "***REDACTED***" in msg


def test_models_upstream_non_json_body_redacted():
    """Upstream /models 200-but-not-JSON must redact the body before echoing it."""
    client, _ = _make_client(lambda req: httpx.Response(
        200, content=b"oops Authorization: Bearer leaked-non-json-9999",
        headers={"content-type": "application/json"},
    ))
    r = client.get("/claude/v1/models")
    assert r.status_code == 502
    msg = r.json()["error"]["message"]
    assert "leaked-non-json-9999" not in msg
    assert "***REDACTED***" in msg


# ---------- /claude/healthz ----------

def test_healthz_upstream_ok():
    client, _ = _make_client(lambda req: _models_response([
        {"id": "claude-x", "vendor": "Anthropic", "capabilities": {"type": "chat"}},
        {"id": "claude-y", "vendor": "anthropic", "capabilities": {"type": "chat"}},
        {"id": "gpt-x", "vendor": "OpenAI", "capabilities": {"type": "chat"}},
    ]))
    r = client.get("/claude/healthz")
    body = r.json()
    assert body["ok"] is True
    assert body["upstream_ok"] is True
    assert body["upstream_status"] == 200
    # Case-insensitive vendor match — both "Anthropic" and "anthropic" count.
    assert body["anthropic_models"] == 2
    assert "hint" not in body


def test_healthz_upstream_not_ok():
    client, _ = _make_client(lambda req: httpx.Response(503, content=b"down"))
    r = client.get("/claude/healthz")
    body = r.json()
    assert body["ok"] is True
    assert body["upstream_ok"] is False
    assert body["upstream_status"] == 503
    assert body["anthropic_models"] == 0
    assert "hint" not in body  # hint only for 401/403


def test_healthz_upstream_token_expired_hint():
    """401/403 from upstream: include actionable hint pointing at extract-token.ps1."""
    client, _ = _make_client(lambda req: httpx.Response(401, content=b"bad token"))
    body = client.get("/claude/healthz").json()
    assert body["upstream_ok"] is False
    assert body["upstream_status"] == 401
    assert "extract-token.ps1" in body["hint"]


def test_healthz_upstream_200_non_json_body_is_not_ok():
    """Regression: a 200 with malformed JSON must NOT report upstream_ok=True."""
    client, _ = _make_client(lambda req: httpx.Response(
        200, content=b"<html>captive portal</html>",
        headers={"content-type": "text/html"},
    ))
    body = client.get("/claude/healthz").json()
    assert body["upstream_ok"] is False
    assert body["upstream_status"] == 200
    assert body["anthropic_models"] == 0


def test_healthz_upstream_exception():
    def handler(req):
        raise httpx.ConnectError("kaput")
    client, _ = _make_client(handler)
    r = client.get("/claude/healthz")
    body = r.json()
    assert body["ok"] is True
    assert body["upstream_ok"] is False
    assert body["upstream_status"] is None


def test_healthz_compatibility_alias():
    client, _ = _make_client(lambda req: _models_response([]))
    assert client.get("/healthz").status_code == 200


def test_claude_v1_routes_are_primary_namespace():
    def handler(req):
        if req.url.path == "/v1/messages":
            return httpx.Response(200, json={"id": "msg_1"})
        return _models_response([
            {"id": "claude-x", "vendor": "Anthropic", "capabilities": {"type": "chat"}},
        ])

    client, _ = _make_client(handler)

    messages = client.post("/claude/v1/messages", json={"model": "claude-x", "messages": []})
    models = client.get("/claude/v1/models")

    assert messages.status_code == 200
    assert messages.json() == {"id": "msg_1"}
    assert models.status_code == 200
    assert models.json()["data"][0]["id"] == "claude-x"
    assert client.post("/v1/messages", json={"model": "claude-x", "messages": []}).status_code == 404
    assert client.get("/v1/models").status_code == 404
