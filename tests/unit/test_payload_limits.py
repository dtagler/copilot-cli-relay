"""Request-byte limits, terminal Codex compatibility, and early compaction policy."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

import copilot_cli_relay.request_diagnostics as diagnostics
from copilot_cli_relay.codex_proxy import (
    _codex_model_info,
    _parse_codex_request,
    proxy_codex_models,
    proxy_codex_responses,
    proxy_codex_responses_compact,
)
from copilot_cli_relay.config import ConfigError, Settings, reset_settings_for_tests
from copilot_cli_relay.errors import openai_upstream_error_fields
from copilot_cli_relay.limits import RequestLimitsMiddleware
from copilot_cli_relay.proxy_shared import MAX_UPSTREAM_ERROR_BYTES
from copilot_cli_relay.request_diagnostics import _json_size, log_codex_request_size, model_log_label


@pytest.fixture(autouse=True)
def settings():
    value = Settings(
        proxy_port=4141, github_token="gho_fixture", api_base="https://upstream.test",
        integration_id="fixture", editor_version="fixture", log_level="info", log_bodies=False,
    )
    reset_settings_for_tests(value)
    yield value
    reset_settings_for_tests(None)


def make_client(handler):
    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            app.state.http_client = client
            yield

    return TestClient(Starlette(
        lifespan=lifespan, middleware=[Middleware(RequestLimitsMiddleware)], routes=[
            Route("/codex/v1/responses", proxy_codex_responses, methods=["POST"]),
            Route("/codex/v1/responses/compact", proxy_codex_responses_compact, methods=["POST"]),
            Route("/codex/v1/models", proxy_codex_models),
        ],
    ))


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("path", ["/codex/v1/responses", "/codex/v1/responses/compact", "/claude/v1/messages"])
@pytest.mark.parametrize("streaming", [False, True])
def test_local_upload_overflow_is_terminal_only_for_opted_in_codex(settings, terminal, path, streaming, caplog):
    reset_settings_for_tests(replace(settings, max_request_bytes=32, codex_terminal_payload_errors=terminal))

    def forbidden(_request):
        pytest.fail("Oversized upload reached upstream.")

    with make_client(forbidden) as client:
        response = client.post(path, json={"stream": streaming, "input": "private-input-sentinel" * 10})
    expected_status = 400 if terminal and path.startswith("/codex/") else 413
    assert response.status_code == expected_status
    assert "private-input-sentinel" not in response.text + caplog.text
    assert "origin=relay_upload" in caplog.text
    assert f"status={expected_status}" in caplog.text
    assert "limit_bytes=32" in caplog.text
    if path.startswith("/codex/"):
        assert response.json()["error"]["code"] == "request_too_large"
        assert response.headers["x-relay-original-status"] == "413"
        assert response.headers["x-relay-error-origin"] == "relay_upload"
        assert response.headers["x-relay-request-id"] in caplog.text
        assert "Retry-After" not in response.headers
        assert "32 bytes" in response.text
    else:
        assert response.json() == {
            "type": "error", "error": {"type": "invalid_request_error", "message": "Request body too large."},
        }


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("protocol", ["claude", "codex"])
async def test_chunked_overflow_compatibility(settings, terminal, protocol, caplog):
    reset_settings_for_tests(replace(settings, max_request_bytes=4, codex_terminal_payload_errors=terminal))
    chunks = [b"123", b"45"]
    sent = []

    async def receive():
        return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}

    async def send(message):
        sent.append(message)

    async def forbidden(*_args):
        pytest.fail("Chunked oversized upload reached handler.")

    middleware = RequestLimitsMiddleware(forbidden)
    await middleware(
        {"type": "http", "path": f"/{protocol}/v1/responses", "method": "POST", "headers": []},
        receive, send,
    )
    assert sent[0]["status"] == (400 if terminal and protocol == "codex" else 413)
    assert middleware.active == 0
    assert "declared_bytes=-1 observed_bytes=5 limit_bytes=4" in caplog.text


@pytest.mark.parametrize("over", [False, True])
@pytest.mark.parametrize("declared", [False, True])
async def test_real_default_32mib_boundary(settings, over, declared):
    assert settings.max_request_bytes == 32 * 1024 * 1024
    total = settings.max_request_bytes + int(over)
    remaining = total
    sent = []
    received_by_handler = []

    async def receive():
        nonlocal remaining
        if remaining:
            size = min(remaining, 1024 * 1024)
            remaining -= size
            return {"type": "http.request", "body": b"x" * size, "more_body": remaining > 0}
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    async def handler(scope, receive, send):
        assert not over
        message = await receive()
        received_by_handler.append(len(message["body"]))
        await Response(status_code=204)(scope, receive, send)

    headers = [(b"content-length", str(total).encode())] if declared else []
    middleware = RequestLimitsMiddleware(handler)
    await middleware(
        {"type": "http", "path": "/codex/v1/responses", "method": "POST", "headers": headers},
        receive, send,
    )
    assert sent[0]["status"] == (413 if over else 204)
    assert received_by_handler == ([] if over else [total])
    assert middleware.active == 0
    if over and declared:
        assert remaining == total  # Early rejection without buffering the body.


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("route,streaming", [
    ("/codex/v1/responses", False), ("/codex/v1/responses", True),
    ("/codex/v1/responses/compact", False),
])
@pytest.mark.parametrize("status,code", [(400, "context_length_exceeded"), (413, "context_length_exceeded")])
def test_upstream_size_errors_keep_distinct_semantics(settings, terminal, route, streaming, status, code, caplog):
    reset_settings_for_tests(replace(settings, codex_terminal_payload_errors=terminal))
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(status, json={
            "error": {"type": "invalid_request_error", "code": code, "param": "input", "message": "private-sentinel"},
        }, headers={"retry-after": "8"})

    with make_client(upstream) as client, caplog.at_level(logging.INFO):
        response = client.post(route, json={"model": "fixture", "stream": streaming, "input": "fixture"})
    assert len(calls) == 1
    assert response.status_code == (400 if terminal and status == 413 else status)
    assert response.headers["x-relay-original-status"] == str(status)
    request_id = response.headers["x-relay-request-id"]
    assert request_id == calls[0].headers["x-request-id"]
    assert f"request_id={request_id}" in caplog.text
    assert "private-sentinel" not in caplog.text
    if status == 413:
        assert response.json()["error"]["code"] == "request_too_large"
        assert response.headers["x-relay-error-origin"] == "upstream_payload"
        assert "413" in response.json()["error"]["message"]
        assert "retry-after" not in response.headers
        assert "private-sentinel" not in response.text
    else:
        assert response.json()["error"]["code"] == "context_length_exceeded"
        assert response.json()["error"]["param"] == "input"
        assert response.headers["x-relay-error-origin"] == "upstream_context"
        assert response.headers["retry-after"] == "8"


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
@pytest.mark.parametrize("streaming", [False, True])
def test_compatibility_does_not_change_other_http_statuses(settings, status, streaming):
    reset_settings_for_tests(replace(settings, codex_terminal_payload_errors=True))
    with make_client(lambda _request: httpx.Response(status, json={
        "error": {"code": "model_not_found", "param": "model", "message": "fixture"},
    }, headers={"retry-after": "3", "www-authenticate": "fixture"})) as client:
        response = client.post("/codex/v1/responses", json={"model": "fixture", "stream": streaming})
    assert response.status_code == status
    assert response.json()["error"]["code"] == "model_not_found"
    assert response.json()["error"]["param"] == "model"
    assert response.headers["retry-after"] == "3"
    assert response.headers["www-authenticate"] == "fixture"
    assert response.headers["x-relay-error-origin"] == "upstream"


@pytest.mark.parametrize("body", [
    b"not-json", b"\xff", b"[]", b'{"error":[]}', b'{"error":null}',
    b'{"error":{"code":[],"param":{}}}', b'{"error":{"code":1,"param":false}}',
    b'{"error":{"code":"private-code","param":"private-parameter"}}',
    b"[" * 2000 + b"]" * 2000,
    b'{"error":{"code":"context_length_exceeded","param":"input"}}' + b" " * MAX_UPSTREAM_ERROR_BYTES,
], ids=[
    "not-json", "invalid-utf8", "array", "error-array", "error-null", "nested-fields",
    "non-string-fields", "unknown-fields", "deep-json", "over-cap",
])
def test_untrusted_or_truncated_error_fields_are_not_preserved(body):
    assert openai_upstream_error_fields(body, max_bytes=MAX_UPSTREAM_ERROR_BYTES) == (None, None)


def test_exact_error_cap_is_not_assumed_complete():
    body = b'{"error":{"code":"context_length_exceeded","param":"input"}}'
    assert openai_upstream_error_fields(body, max_bytes=len(body)) == (None, None)
    assert openai_upstream_error_fields(body, max_bytes=len(body) + 1) == ("context_length_exceeded", "input")


@pytest.mark.parametrize("streaming", [False, True])
def test_unreadable_upstream_413_is_still_terminal_and_closed(settings, streaming):
    reset_settings_for_tests(replace(settings, codex_terminal_payload_errors=True))

    class Broken(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            raise httpx.ReadError("fixture")
            yield b"unreachable"

        async def aclose(self):
            self.closed = True

    stream = Broken()
    with make_client(lambda _request: httpx.Response(413, stream=stream)) as client:
        response = client.post("/codex/v1/responses", json={"model": "fixture", "stream": streaming})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "request_too_large"
    assert stream.closed


def test_size_diagnostics_are_content_free_and_do_not_mutate_history(caplog):
    marker = "private-sentinel"
    parsed = {
        "model": f"gpt-{marker}", "reasoning": {"effort": "max"},
        "instructions": marker, "previous_response_id": marker,
        "tools": [{"type": "function", "name": marker, "description": marker}],
        "input": [
            {"type": "reasoning", "encrypted_content": marker},
            {"role": "user", "content": [{"type": "input_image", "image_url": marker}]},
            {"type": "function_call", "name": marker, "arguments": marker},
            {"type": "function_call_output", "call_id": marker, "output": marker},
            {"type": marker, marker: marker},
            {"type": []},
        ],
    }
    original_input = json.dumps(parsed["input"])
    incoming = json.dumps(parsed, indent=2).encode()
    with caplog.at_level(logging.INFO):
        outgoing, model, _, _ = _parse_codex_request(
            incoming, request_id="fixture-id", limit_bytes=len(incoming),
        )
    assert model == f"gpt-{marker}"  # Routing is not changed by the log label.
    assert json.dumps(json.loads(outgoing)["input"]) == original_input
    assert marker not in caplog.text
    assert "model_family=gpt reasoning_effort=max" in caplog.text
    assert "near_limit=1" in caplog.text
    assert f"incoming_bytes={len(incoming)} outgoing_bytes={len(outgoing)}" in caplog.text
    assert f"reasoning_json_bytes={_json_size(parsed['input'][0])}" in caplog.text
    assert f"tool_results_json_bytes={_json_size(parsed['input'][3])}" in caplog.text
    assert f"tools_json_bytes={_json_size(parsed['tools'])}" in caplog.text


@pytest.mark.parametrize("body", [
    b"invalid", b"[]", b'{"input":"hello","reasoning":{"effort":"private"}}',
    b'{"model":"claude-fixture","input":[{"type":"message","content":"hello"}]}',
    b"[" * 2000 + b"]" * 2000,
])
def test_malformed_and_alternate_requests_still_forward_with_diagnostics(body, caplog):
    with caplog.at_level(logging.INFO):
        outgoing, _, _, _ = _parse_codex_request(body, request_id="fixture", limit_bytes=32_000)
    assert outgoing == body
    assert "request_size request_id=fixture" in caplog.text
    assert "private" not in caplog.text


def test_diagnostics_handle_unserializable_values_and_disabled_info(caplog):
    with caplog.at_level(logging.INFO):
        log_codex_request_size({"input": [object()]}, incoming_bytes=10, outgoing_bytes=10,
                               limit_bytes=100, request_id="fixture")
    assert "estimates_available=0" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        log_codex_request_size({}, incoming_bytes=10, outgoing_bytes=10, limit_bytes=100, request_id="fixture")
    assert not caplog.records
    assert model_log_label(None) == "other"
    assert _json_size("\ud800") > 0


@pytest.mark.parametrize("parsed", [
    {"input": [""] * 10_000},
    {"input": [{"type": "message", "content": [""] * 10_000}]},
    {"input": [""] * 5, "tools": [""] * 10_000},
    {"input": [""] * 5, "tools": [], "instructions": [""] * 10_000},
])
def test_diagnostic_work_budget_is_shared_and_bounds_nested_traversal(monkeypatch, caplog, parsed):
    monkeypatch.setattr(diagnostics, "MAX_DIAGNOSTIC_JSON_CHUNKS", 8)
    original = json.JSONEncoder.iterencode
    chunks = 0

    def counted(self, value, *args, **kwargs):
        nonlocal chunks
        for chunk in original(self, value, *args, **kwargs):
            chunks += 1
            assert chunks <= 9, "Diagnostics exceeded the shared traversal budget."
            yield chunk

    monkeypatch.setattr(json.JSONEncoder, "iterencode", counted)
    with caplog.at_level(logging.INFO):
        log_codex_request_size(parsed, incoming_bytes=30_000, outgoing_bytes=30_000,
                               limit_bytes=32_000, request_id="fixture")
    assert chunks == 9
    assert "estimates_available=0" in caplog.text
    assert f"input_items={len(parsed['input'])}" in caplog.text
    assert "incoming_bytes=30000 outgoing_bytes=30000" in caplog.text
    assert "messages_json_bytes=0" in caplog.text
    assert "tools_json_bytes=0 instructions_json_bytes=0" in caplog.text


@pytest.mark.parametrize("value", [True, False, None, "300000", 1.5, 0, -1, 10_000_001, {}, []])
def test_compaction_overrides_reject_invalid_types_and_values(monkeypatch, value):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_fixture")
    monkeypatch.setenv("COPILOT_CODEX_MODEL_OVERRIDES", json.dumps({
        "fixture": {"auto_compact_token_limit": value},
    }))
    with pytest.raises(ConfigError, match="COPILOT_CODEX_MODEL_OVERRIDES"):
        Settings.from_env()


@pytest.mark.parametrize("window,requested,expected", [
    (1_050_000, 300_000, 300_000), (100_000, 300_000, 90_000),
    (None, 300_000, 7372), (1, 1, 1), (10_000_000, 10_000_000, 9_000_000),
])
def test_compaction_policy_is_per_model_bounded_and_preserves_window(monkeypatch, window, requested, expected):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_fixture")
    monkeypatch.setenv("COPILOT_CODEX_MODEL_OVERRIDES", json.dumps({
        "fixture": {"auto_compact_token_limit": requested, "supports_search_tool": True},
    }))
    monkeypatch.setenv("COPILOT_CODEX_TERMINAL_PAYLOAD_ERRORS", "1")
    monkeypatch.delenv("COPILOT_MAX_REQUEST_BYTES", raising=False)
    settings = Settings.from_env()
    reset_settings_for_tests(settings)
    assert settings.max_request_bytes == 32 * 1024 * 1024
    assert settings.codex_terminal_payload_errors is True
    model = {"id": "fixture", "supported_endpoints": ["/responses"], "capabilities": {
        "limits": {"max_context_window_tokens": window},
    }}
    with make_client(lambda _request: httpx.Response(200, json={"data": [model]})) as client:
        info = client.get("/codex/v1/models").json()["models"][0]
    assert info["auto_compact_token_limit"] == expected
    assert info["context_window"] == info["max_context_window"] == (window or 8192)
    assert info["supports_search_tool"] is True
    assert "auto_compact_token_limit" not in _codex_model_info(model, "other")
    assert settings.codex_model_overrides["fixture"]["auto_compact_token_limit"] == requested


def test_payload_compatibility_and_compaction_are_opt_in(monkeypatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_fixture")
    for key in ("COPILOT_CODEX_TERMINAL_PAYLOAD_ERRORS", "COPILOT_CODEX_MODEL_OVERRIDES",
                "COPILOT_ENABLE_SYNTHETIC_COMPACTION", "COPILOT_MAX_REQUEST_BYTES"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings.from_env()
    assert settings.codex_terminal_payload_errors is False
    assert settings.enable_synthetic_compaction is False
    assert settings.codex_model_overrides == {}
    assert settings.max_request_bytes == 32 * 1024 * 1024
    monkeypatch.setenv("COPILOT_MAX_REQUEST_BYTES", "16777216")
    assert Settings.from_env().max_request_bytes == 16 * 1024 * 1024
