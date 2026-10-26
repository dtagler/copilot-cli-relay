"""Regression tests for the four findings from the October 2026 project review."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager, contextmanager

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import copilot_cli_relay.claude_proxy as claude_mod
import copilot_cli_relay.codex_proxy as codex_mod
from copilot_cli_relay.config import Settings, reset_settings_for_tests
from copilot_cli_relay.headers import build_claude_outbound_headers
from copilot_cli_relay.logging_setup import MAX_BODY_BYTES, redact_bytes
from copilot_cli_relay.proxy_shared import MAX_UPSTREAM_ERROR_BYTES, read_bounded


@pytest.fixture
def client_factory():
    @contextmanager
    def make_client(handler, *, log_bodies=False):
        reset_settings_for_tests(Settings(
            proxy_port=4141, github_token="gho_test",
            api_base="https://upstream.test", integration_id="copilot-developer-cli",
            editor_version="copilot-cli-relay/test", log_level="info", log_bodies=log_bodies,
            # Preserve every synthetic fallback regression under explicit opt-in.
            enable_synthetic_compaction=True,
        ))

        @asynccontextmanager
        async def lifespan(app):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
                app.state.http_client = upstream
                yield

        app = Starlette(lifespan=lifespan, routes=[
            Route("/claude/v1/messages", claude_mod.proxy_claude_messages, methods=["POST"]),
            Route("/claude/v1/models", claude_mod.proxy_claude_models),
            Route("/claude/healthz", claude_mod.claude_healthz),
            Route("/healthz", claude_mod.claude_healthz),
            Route("/codex/v1/responses", codex_mod.proxy_codex_responses, methods=["POST"]),
            Route("/codex/v1/responses/compact", codex_mod.proxy_codex_responses_compact, methods=["POST"]),
            Route("/codex/v1/models", codex_mod.proxy_codex_models),
            Route("/codex/healthz", codex_mod.codex_healthz),
        ])
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                yield client
        finally:
            reset_settings_for_tests(None)

    return make_client


def _boundary_body(size):
    # Both values are synthetic. At cap + 2, the second closing quote is cut.
    prefix = b'{"trace":"keep-this","secret":"'
    middle = b'","password":"boundary-canary'
    return prefix + b"x" * (size - len(prefix + middle) - 2) + middle + b'"}'


@pytest.mark.parametrize("offset", [-1, 0, 2])
def test_body_logging_size_boundary_is_fail_closed(offset):
    body = _boundary_body(MAX_BODY_BYTES + offset)
    assert len(body) == MAX_BODY_BYTES + offset
    safe = redact_bytes(body)
    assert b"boundary-canary" not in safe
    if offset < 0:
        assert b"keep-this" in safe
        assert safe.count(b"***REDACTED***") == 2
    else:
        assert b"omitted" in safe
        assert b"keep-this" not in safe
    assert len(safe) <= MAX_BODY_BYTES + len(b"...[truncated]")


def test_body_logging_checks_size_before_shortening_data_uris():
    body = b'{"image":"data:image/png;base64,' + b"A" * (MAX_BODY_BYTES * 2) + b'","password":"canary"}'
    safe = redact_bytes(body)
    assert b"omitted" in safe
    assert b"image/png" not in safe
    assert b"canary" not in safe


def test_body_logging_limits_redacted_expansion_and_handles_multibyte_text():
    body = (b'{"password":"abcdef"},' * 700) + '"trace":"caf\u00e9"'.encode()
    assert len(body) < MAX_BODY_BYTES
    safe = redact_bytes(body)
    assert b"abcdef" not in safe
    assert safe.endswith(b"...[truncated]")
    assert len(safe) <= MAX_BODY_BYTES + len(b"...[truncated]")
    assert redact_bytes(b'{"password":"synthetic-secret","trace":"\xffcaf\xc3\xa9"}').decode("utf-8")


class _ErrorStream(httpx.AsyncByteStream):
    def __init__(self, body, fragmented):
        self.body = body
        self.fragmented = fragmented
        self.read_tail = False
        self.closed = False

    async def __aiter__(self):
        if self.fragmented:
            yield self.body[:MAX_UPSTREAM_ERROR_BYTES - 10]
            yield self.body[MAX_UPSTREAM_ERROR_BYTES - 10:]
        else:
            yield self.body
        if len(self.body) >= MAX_UPSTREAM_ERROR_BYTES:
            self.read_tail = True
            raise AssertionError("The capped reader must not consume the remaining stream.")

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("protocol", ["claude", "codex"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("log_bodies", [False, True])
@pytest.mark.parametrize("offset", [-1, 0, 2])
@pytest.mark.parametrize("fragmented", [False, True])
def test_upstream_error_truncation_does_not_expose_secrets(
    client_factory, caplog, protocol, streaming, log_bodies, offset, fragmented,
):
    body = _boundary_body(MAX_UPSTREAM_ERROR_BYTES + offset)
    upstream = _ErrorStream(body, fragmented)
    with client_factory(
        lambda request: httpx.Response(429, stream=upstream, headers={"retry-after": "7"}),
        log_bodies=log_bodies,
    ) as client:
        route = "/claude/v1/messages" if protocol == "claude" else "/codex/v1/responses"
        response = client.post(route, json={"model": "test-model", "stream": streaming, "input": "fixture"})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert response.json()["error"]["type"] == "rate_limit_error"
    assert "boundary-canary" not in response.text
    assert "boundary-canary" not in caplog.text
    if offset < 0:
        assert "keep-this" in response.json()["error"]["message"]
    else:
        assert "omitted" in response.json()["error"]["message"]
    if not log_bodies:
        assert "keep-this" not in caplog.text
    assert not upstream.read_tail
    assert upstream.closed


@pytest.mark.parametrize("fragmented", [False, True])
async def test_bounded_reader_stops_inside_large_chunk_without_reading_tail(fragmented):
    class FakeResponse:
        is_stream_consumed = False
        headers = {}

        async def aiter_raw(self):
            if fragmented:
                yield b"prefix"
            yield b"x" * (1024 * 1024)
            pytest.fail("Reader consumed a tail after reaching its cap")

    result = await read_bounded(FakeResponse(), 32)
    assert result == (b"prefix" + b"x" * 26 if fragmented else b"x" * 32)


def _summary_output():
    return [{
        "id": "message_fixture", "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": "Preserve this task summary.", "annotations": []}],
    }]


@pytest.mark.parametrize("status", [None, "failed", "incomplete", "in_progress", "queued", "cancelled", "unknown"])
def test_synthetic_compact_rejects_non_completed_status(client_factory, status):
    _assert_invalid_compact(client_factory, {"status": status, "output": _summary_output()})


@pytest.mark.parametrize("result", [
    None, [], "not-an-object", {},
    {"output": []},
    {"status": "completed"},
    {"status": "completed", "output": None},
    {"status": "completed", "output": {}},
    {"status": "completed", "output": []},
    {"status": "completed", "output": [{"type": "reasoning", "summary": []}]},
    {"status": "completed", "output": [{"type": "function_call", "call_id": "fixture"}]},
    {"status": "completed", "output": [None, *_summary_output()]},
    {"status": "completed", "output": [*_summary_output(), None]},
    {"status": "completed", "output": [*_summary_output(), {"type": "function_call", "call_id": "fixture"}]},
    {"status": "completed", "output": [{"type": "reasoning", "status": "incomplete"}, *_summary_output()]},
    {"status": "completed", "error": {"message": "private-upstream-detail"}, "output": _summary_output()},
    {"status": "completed", "incomplete_details": {"reason": "fixture"}, "output": _summary_output()},
])
def test_synthetic_compact_rejects_invalid_shape_or_failure_details(client_factory, result):
    _assert_invalid_compact(client_factory, result)


@pytest.mark.parametrize("change", [
    {"role": "user"}, {"status": None}, {"status": "incomplete"}, {"type": "unknown"}, {"phase": "unknown"},
    {"content": None}, {"content": []}, {"content": "not-an-array"},
    {"content": [None]},
    {"content": [{"type": "output_text", "text": None}]},
    {"content": [{"type": "output_text", "text": " \r\n\t"}]},
    {"content": [{"type": "refusal", "refusal": "private-upstream-detail"}]},
    {"content": [*_summary_output()[0]["content"], {"type": "refusal", "refusal": "fixture"}]},
])
def test_synthetic_compact_rejects_unusable_summary(client_factory, change):
    output = _summary_output()
    output[0].update(change)
    _assert_invalid_compact(client_factory, {"status": "completed", "output": output})


def _assert_invalid_compact(client_factory, result):
    def handler(request):
        if request.url.path == "/responses/compact":
            return httpx.Response(404)
        # Encode null explicitly: httpx's json=None means no JSON body.
        return httpx.Response(200, content=json.dumps(result).encode())

    with client_factory(handler) as client:
        response = client.post("/codex/v1/responses/compact", json={"model": "test-model", "input": "fixture"})
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "server_error"
    assert "response.compaction" not in response.text
    assert "private-upstream-detail" not in response.text


@pytest.mark.parametrize("change", [
    {"summary": 7}, {"summary": [7]},
    {"summary": [{"type": "summary_text", "text": 7}]},
    {"summary": [{"type": "unknown", "text": "fixture"}]},
    {"encrypted_content": []}, {"content": 7}, {"content": [7]},
    {"content": [{"type": "reasoning_text", "text": False}]},
    {"id": 7},
])
def test_synthetic_compact_rejects_nested_reasoning_metadata(client_factory, change):
    reasoning = {"id": "reason_fixture", "type": "reasoning", "summary": [], **change}
    _assert_invalid_compact(client_factory, {"status": "completed", "output": [reasoning, *_summary_output()]})


@pytest.mark.parametrize("annotations", [
    None, 7, [7], [{}], [{"type": "unknown"}],
    [{"type": "url_citation", "url": 7}],
    [{"type": "url_citation", "url": "https://example.test", "title": "fixture", "start_index": False, "end_index": 4}],
])
def test_synthetic_compact_rejects_nested_annotations(client_factory, annotations):
    output = _summary_output()
    output[0]["content"][0]["annotations"] = annotations
    _assert_invalid_compact(client_factory, {"status": "completed", "output": output})


@pytest.mark.parametrize("logprobs", [
    7, [7], [{"token": 7, "logprob": -1}],
    [{"token": "x", "logprob": True}],
    [{"token": "x", "logprob": -1, "bytes": [300]}],
    [{"token": "x", "logprob": -1, "top_logprobs": 7}],
    [{"token": "x", "logprob": -1, "top_logprobs": [7]}],
])
def test_synthetic_compact_rejects_nested_logprobs(client_factory, logprobs):
    output = _summary_output()
    output[0]["content"][0]["logprobs"] = logprobs
    _assert_invalid_compact(client_factory, {"status": "completed", "output": output})


@pytest.mark.parametrize("usage", [
    7, [], {"total_tokens": float("nan")}, {"total_tokens": float("inf")},
    {"total_tokens": True}, {"total_tokens": -1}, {"input_tokens": 1.5},
    {"input_tokens_details": 7}, {"input_tokens_details": {"cached_tokens": False}},
    {"output_tokens_details": {"reasoning_tokens": "1"}},
])
def test_synthetic_compact_rejects_nested_usage(client_factory, usage):
    _assert_invalid_compact(client_factory, {"status": "completed", "output": _summary_output(), "usage": usage})


@pytest.mark.parametrize("text", ["\ud800", "\udfff"])
def test_synthetic_compact_serialization_failure_is_a_protocol_error(client_factory, text):
    output = _summary_output()
    output[0]["content"][0]["text"] = text
    _assert_invalid_compact(client_factory, {"status": "completed", "output": output})


def test_synthetic_compact_rejects_overflowed_json_number(client_factory):
    raw = json.dumps({"status": "completed", "output": _summary_output(), "usage": {"total_tokens": "overflow"}})
    raw = raw.replace('"overflow"', "1e9999").encode()
    with client_factory(lambda request: httpx.Response(
        404 if request.url.path == "/responses/compact" else 200, content=raw,
    )) as client:
        response = client.post("/codex/v1/responses/compact", json={"model": "test-model"})
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "server_error"


def test_synthetic_compact_preserves_valid_nested_metadata(client_factory):
    output = [{
        "type": "reasoning", "id": "reason_fixture", "status": "completed",
        "summary": [{"type": "summary_text", "text": "Synthetic reasoning summary."}],
        "content": [{"type": "reasoning_text", "text": "Synthetic content."}],
        "encrypted_content": "synthetic-fixture-ciphertext",
    }, *_summary_output()]
    output[1]["phase"] = "final_answer"
    output[1]["content"][0]["annotations"] = [
        {"type": "file_citation", "file_id": "file_fixture", "filename": "fixture.txt", "index": 0},
        {"type": "file_path", "file_id": "file_fixture", "index": 0},
        {"type": "url_citation", "url": "https://example.test", "title": "fixture", "start_index": 0, "end_index": 1},
        {
            "type": "container_file_citation", "container_id": "container_fixture",
            "file_id": "file_fixture", "filename": "fixture.txt", "start_index": 0, "end_index": 1,
        },
    ]
    output[1]["content"][0]["logprobs"] = [{
        "token": "x", "bytes": [120], "logprob": -0.5,
        "top_logprobs": [{"token": "y", "bytes": [121], "logprob": -1.2}],
    }]
    usage = {
        "input_tokens": 2, "output_tokens": 3, "total_tokens": 5,
        "input_tokens_details": {"cached_tokens": 1, "cache_write_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 1},
    }
    with client_factory(lambda request: httpx.Response(
        404 if request.url.path == "/responses/compact" else 200,
        json={"status": "completed", "output": output, "usage": usage},
    )) as client:
        response = client.post("/codex/v1/responses/compact", json={"model": "test-model"})
    assert response.status_code == 200
    assert response.json()["output"] == output
    assert response.json()["usage"] == usage


@pytest.mark.parametrize("status", [None, "completed"])
@pytest.mark.parametrize("usage", [None, {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}])
def test_synthetic_compact_preserves_valid_reasoning_summary_and_usage(client_factory, status, usage):
    reasoning = {"type": "reasoning", "summary": []}
    if status is not None:
        reasoning["status"] = status
    output = [reasoning, *_summary_output()]

    def handler(request):
        if request.url.path == "/responses/compact":
            return httpx.Response(404)
        return httpx.Response(200, json={"status": "completed", "output": output, "usage": usage})

    with client_factory(handler) as client:
        response = client.post("/codex/v1/responses/compact", json={"model": "test-model", "input": "fixture"})
    assert response.status_code == 200
    assert response.json()["object"] == "response.compaction"
    assert response.json()["output"] == output
    assert response.json()["usage"] == usage


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_synthetic_compact_does_not_turn_http_failure_into_success(client_factory, status):
    def handler(request):
        if request.url.path == "/responses/compact":
            return httpx.Response(404)
        return httpx.Response(
            status, json={"status": "completed", "output": _summary_output()},
            headers={"retry-after": "7"},
        )

    with client_factory(handler) as client:
        response = client.post("/codex/v1/responses/compact", json={"model": "test-model", "input": "fixture"})
    assert response.status_code == (502 if status < 400 else status)
    assert "error" in response.json()
    if status >= 400:
        assert response.headers["retry-after"] == "7"


@pytest.mark.parametrize("protocol", ["claude", "codex"])
@pytest.mark.parametrize("payload", [None, [], "invalid", {}, {"data": None}, {"data": 123}, {"data": "invalid"}, {"data": {}}, {"data": False}])
def test_invalid_catalog_shape_fails_with_protocol_error_and_unhealthy_upstream(client_factory, protocol, payload):
    with client_factory(lambda request: httpx.Response(200, content=json.dumps(payload).encode())) as client:
        catalog = client.get(f"/{protocol}/v1/models")
        health = client.get(f"/{protocol}/healthz")
    assert catalog.status_code == 502
    assert catalog.json()["error"]["type"] == ("api_error" if protocol == "claude" else "server_error")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert health.json()["upstream_status"] == 200
    assert health.json()["upstream_ok"] is False
    assert health.json()["anthropic_models" if protocol == "claude" else "response_models"] == 0


@pytest.mark.parametrize("protocol", ["claude", "codex"])
def test_valid_empty_catalog_is_not_an_upstream_failure(client_factory, protocol):
    with client_factory(lambda request: httpx.Response(200, json={"data": []})) as client:
        catalog = client.get(f"/{protocol}/v1/models")
        health = client.get(f"/{protocol}/healthz")
    assert catalog.status_code == 200
    assert catalog.json()["data"] == []
    assert health.json()["upstream_ok"] is True
    assert health.json()["anthropic_models" if protocol == "claude" else "response_models"] == 0


@pytest.mark.parametrize("vendor", [123, True, ["Anthropic"], {"name": "Anthropic"}, None])
def test_non_string_claude_vendor_is_skipped_without_losing_valid_rows(client_factory, vendor):
    valid = {"vendor": "Anthropic", "id": "claude-valid", "capabilities": {"type": "chat"}}
    with client_factory(lambda request: httpx.Response(
        200, json={"data": [{**valid, "vendor": vendor}, valid]},
    )) as client:
        catalog = client.get("/claude/v1/models")
        health = client.get("/claude/healthz")
    assert catalog.status_code == 200
    assert [model["id"] for model in catalog.json()["data"]] == ["claude-valid"]
    assert health.json()["upstream_ok"] is True
    assert health.json()["anthropic_models"] == 1


@pytest.mark.parametrize("protocol", ["claude", "codex"])
def test_health_success_is_set_only_after_counting_finishes(client_factory, monkeypatch, protocol):
    def fail_count(model):
        raise ValueError("synthetic validation failure")

    module, predicate = (claude_mod, "_is_anthropic") if protocol == "claude" else (codex_mod, "_model_supports_responses")
    monkeypatch.setattr(module, predicate, fail_count)
    with client_factory(lambda request: httpx.Response(200, json={"data": [{}]})) as client:
        health = client.get(f"/{protocol}/healthz")
    assert health.json()["upstream_ok"] is False
    assert health.json()["upstream_status"] == 200
    assert health.json()["anthropic_models" if protocol == "claude" else "response_models"] == 0


@pytest.mark.parametrize("request_id", [None, "relay-request"])
@pytest.mark.parametrize("connection", ["", "X-Request-Id, Copilot-Integration-Id, X-Hop"])
def test_claude_identity_header_collisions_leave_one_authoritative_value(request_id, connection):
    inbound = {"Connection": connection, "X-Hop": "remove", "X-Trace": "keep", "Anthropic-Version": "fixture-version"}
    for name in ("copilot-integration-id", "editor-version", "x-request-id", "authorization", "user-agent"):
        for variant in (name, name.upper(), name.title()):
            inbound[variant] = "untrusted-client-value"
    headers = build_claude_outbound_headers(
        inbound, bearer_token="gho_test", integration_id="relay-integration",
        editor_version="relay-editor", request_id=request_id,
    )
    outgoing = httpx.Request("POST", "https://upstream.test/messages", headers=headers).headers
    assert outgoing.get_list("authorization") == ["Bearer gho_test"]
    assert outgoing.get_list("copilot-integration-id") == ["relay-integration"]
    assert outgoing.get_list("editor-version") == ["relay-editor"]
    assert outgoing.get_list("user-agent") == ["relay-editor"]
    assert len(outgoing.get_list("x-request-id")) == 1
    assert outgoing["x-request-id"] != "untrusted-client-value"
    if request_id is not None:
        assert outgoing["x-request-id"] == request_id
    assert outgoing.get_list("anthropic-version") == ["fixture-version"]
    assert outgoing["x-trace"] == "keep"
    if connection:
        assert "x-hop" not in outgoing
