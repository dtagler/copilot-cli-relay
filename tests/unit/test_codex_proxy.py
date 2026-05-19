from __future__ import annotations

import gzip
import json

import httpx
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import copilot_cli_relay.codex_proxy as codex_mod
from copilot_cli_relay.codex_proxy import (
    codex_healthz,
    proxy_codex_models,
    proxy_codex_responses,
    proxy_codex_responses_compact,
)
from copilot_cli_relay.config import Settings, reset_settings_for_tests


def _settings(log_bodies: bool = True) -> Settings:
    return Settings(
        proxy_port=4141,
        github_token="gho_test",
        api_base="https://upstream.test",
        integration_id="copilot-developer-cli",
        editor_version="copilot-cli-relay/test",
        log_level="info",
        log_bodies=log_bodies,
        codex_integration_id="copilot-developer-cli",
        codex_editor_version="vscode/1.99.0",
        codex_plugin_version="copilot-chat/0.43.2026033101",
        codex_user_agent="GitHubCopilotChat/0.43.2026033101",
        codex_github_api_version="2026-01-09",
        codex_session_id="session-1",
        codex_machine_id="a" * 64,
    )


def _make_client(handler, *, log_bodies: bool = True):
    reset_settings_for_tests(_settings(log_bodies=log_bodies))
    app = Starlette(
        routes=[
            Route("/codex/v1/responses", proxy_codex_responses, methods=["POST"]),
            Route("/codex/v1/responses/compact", proxy_codex_responses_compact, methods=["POST"]),
            Route("/codex/v1/models", proxy_codex_models, methods=["GET"]),
            Route("/codex/healthz", codex_healthz, methods=["GET"]),
        ],
    )
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TestClient(app), app


def test_parse_codex_request_strips_rejected_fields_and_tools(caplog):
    raw = json.dumps({
        "model": "gpt-5.5",
        "input": "hi",
        "stream": True,
        "previous_response_id": "resp_old",
        "tools": [
            {"type": "image_generation"},
            {"type": "web_search"},
            {"type": "function", "name": "shell"},
        ],
        "tool_choice": {"type": "image_generation"},
    }).encode()

    body, model, streaming, initiator = codex_mod._parse_codex_request(raw, request_id="rid")

    assert model == "gpt-5.5"
    assert streaming is True
    assert initiator == "user"
    parsed = json.loads(body)
    assert "previous_response_id" not in parsed
    assert parsed["tools"] == [{"type": "web_search"}, {"type": "function", "name": "shell"}]
    assert "tool_choice" not in parsed
    assert "stripped previous_response_id" in caplog.text
    assert "image_generation" in caplog.text


def test_parse_codex_request_no_rewrite_returns_original_bytes():
    raw = b'{"model":"gpt-5.5","input":"hi","stream":false}'
    body, model, streaming, initiator = codex_mod._parse_codex_request(raw, request_id="rid")
    assert body is raw
    assert model == "gpt-5.5"
    assert streaming is False
    assert initiator == "user"


def test_codex_non_streaming_success_headers_and_body_rewrite():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "resp_1", "object": "response"})

    client, _ = _make_client(handler)
    response = client.post(
        "/codex/v1/responses",
        json={
            "model": "gpt-5.5",
            "input": "hi",
            "previous_response_id": "resp_old",
            "tools": [{"type": "image_generation"}, {"type": "function", "name": "shell"}],
        },
        headers={
            "Authorization": "Bearer leaked",
            "x-api-key": "sk-leaked",
            "x-codex-turn-metadata": "local-session",
        },
    )

    assert response.status_code == 200
    assert captured["url"] == "https://upstream.test/responses"
    assert captured["headers"]["authorization"] == "Bearer gho_test"
    assert captured["headers"]["copilot-integration-id"] == "copilot-developer-cli"
    assert captured["headers"]["editor-version"] == "vscode/1.99.0"
    assert captured["headers"]["editor-plugin-version"] == "copilot-chat/0.43.2026033101"
    assert captured["headers"]["openai-intent"] == "conversation-panel"
    assert captured["headers"]["x-interaction-type"] == "conversation-panel"
    assert captured["headers"]["x-github-api-version"] == "2026-01-09"
    assert captured["headers"]["vscode-sessionid"] == "session-1"
    assert captured["headers"]["vscode-machineid"] == "a" * 64
    assert captured["headers"]["x-initiator"] == "user"
    assert captured["headers"]["accept"] == "application/json"
    assert "x-api-key" not in captured["headers"]
    assert "x-codex-turn-metadata" not in captured["headers"]
    assert "previous_response_id" not in captured["body"]
    assert captured["body"]["tools"] == [{"type": "function", "name": "shell"}]


def test_codex_non_streaming_success_filters_response_headers():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=gzip.compress(b'{"id":"resp_1"}'),
            headers={
                "content-type": "application/json",
                "x-upstream": "keep",
                "set-cookie": "session=leak",
                "server": "upstream-server",
                "connection": "close, x-hop",
                "x-hop": "drop",
                "content-encoding": "gzip",
            },
        )

    client, _ = _make_client(handler)
    response = client.post("/codex/v1/responses", json={"model": "gpt-5.5"})

    keys = {k.lower() for k in response.headers}
    assert response.status_code == 200
    assert response.headers["x-upstream"] == "keep"
    assert "set-cookie" not in keys
    assert "server" not in keys
    assert "connection" not in keys
    assert "x-hop" not in keys
    assert "content-encoding" not in keys


def test_codex_streaming_success_forwards_sse_and_headers():
    chunks = [
        b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"r"}}\n\n',
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://upstream.test/responses"
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b"".join(chunks)),
            headers={"content-type": "text/event-stream", "x-request-id": "upstream-rid"},
        )

    client, _ = _make_client(handler)
    with client.stream("POST", "/codex/v1/responses", json={"model": "gpt-5.5", "stream": True}) as response:
        assert response.status_code == 200
        assert response.headers["x-request-id"] == "upstream-rid"
        body = b"".join(response.iter_bytes())
    assert b"response.completed" in body


def test_codex_streaming_midstream_error_is_openai_sse():
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: response.created\ndata: {"type":"response.created"}\n\n'
            raise httpx.ReadError("stream broke Authorization: Bearer leaked-token-123456789")

        async def aclose(self):
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=BrokenStream(), headers={"content-type": "text/event-stream"})

    client, _ = _make_client(handler)
    with client.stream("POST", "/codex/v1/responses", json={"model": "gpt-5.5", "stream": True}) as response:
        body = b"".join(response.iter_bytes()).decode()

    assert response.status_code == 200
    assert "event: error" in body
    assert '"type":"error"' in body
    assert '"code":"server_error"' in body
    assert '"message":"Upstream stream error:' in body
    assert '"error":{' not in body
    assert "leaked-token" not in body


def test_codex_non_streaming_upstream_error_is_openai_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"slow down", headers={"retry-after": "12"})

    client, _ = _make_client(handler)
    response = client.post("/codex/v1/responses", json={"model": "gpt-5.5"})

    assert response.status_code == 429
    assert response.headers["retry-after"] == "12"
    payload = response.json()
    assert "type" not in payload
    assert payload["error"]["type"] == "rate_limit_error"
    assert "Upstream 429" in payload["error"]["message"]


def test_codex_streaming_pre_error_preserves_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"bad token", headers={"www-authenticate": "Bearer"})

    client, _ = _make_client(handler)
    response = client.post("/codex/v1/responses", json={"model": "gpt-5.5", "stream": True})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["type"] == "authentication_error"


def test_codex_models_filters_to_responses_capable_models():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://upstream.test/models"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "gpt-5.5",
                        "name": "GPT-5.5",
                        "vendor": "OpenAI",
                        "supported_endpoints": ["/responses"],
                    },
                    {"id": "gpt-4o", "vendor": "OpenAI", "supported_endpoints": ["/chat/completions"]},
                    {"id": "claude-opus-4.7", "vendor": "Anthropic", "supported_endpoints": ["/v1/messages"]},
                    {"id": "gpt-hidden", "vendor": "OpenAI", "supported_endpoints": ["/responses"]},
                ]
            },
        )

    client, _ = _make_client(handler)
    response = client.get("/codex/v1/models")

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert payload["data"] == [
        {"id": "gpt-5.5", "object": "model", "created": 0, "owned_by": "OpenAI"},
        {"id": "gpt-hidden", "object": "model", "created": 0, "owned_by": "OpenAI"},
    ]
    assert payload["models"][0]["slug"] == "gpt-5.5"
    assert payload["models"][0]["display_name"] == "GPT-5.5"
    assert payload["models"][0]["supported_in_api"] is True
    assert payload["models"][0]["supported_reasoning_levels"][-1]["effort"] == "xhigh"
    assert payload["models"][1]["slug"] == "gpt-hidden"


def test_codex_models_includes_codex_model_catalog_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{
                    "id": "gpt-5.5",
                    "name": "GPT-5.5",
                    "supported_endpoints": ["/responses"],
                    "capabilities": {"limits": {"max_context_window_tokens": 123_456}},
                }]
            },
        )

    client, _ = _make_client(handler)
    response = client.get("/codex/v1/models")

    assert response.status_code == 200
    assert response.json()["models"] == [{
        "slug": "gpt-5.5",
        "display_name": "GPT-5.5",
        "description": "GPT-5.5 via GitHub Copilot.",
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [
            {"effort": "low", "description": "Fast responses with lighter reasoning"},
            {"effort": "medium", "description": "Balances speed and reasoning depth"},
            {"effort": "high", "description": "Greater reasoning depth"},
            {"effort": "xhigh", "description": "Extra high reasoning depth"},
        ],
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 0,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "availability_nux": None,
        "upgrade": None,
        "base_instructions": (
            "You are Codex, a coding agent. Follow the user's instructions and use available tools safely."
        ),
        "model_messages": None,
        "supports_reasoning_summaries": False,
        "default_reasoning_summary": "none",
        "support_verbosity": True,
        "default_verbosity": "low",
        "apply_patch_tool_type": "freeform",
        "web_search_tool_type": "text",
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "supports_parallel_tool_calls": True,
        "supports_image_detail_original": False,
        "context_window": 123456,
        "max_context_window": 123456,
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [],
        "input_modalities": ["text"],
        "supports_search_tool": True,
    }]
    assert response.json()["data"] == [
        {"id": "gpt-5.5", "object": "model", "created": 0, "owned_by": "github-copilot"}
    ]


def test_codex_models_context_window_falls_back_when_limits_missing_or_malformed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-missing-limits", "supported_endpoints": ["/responses"]},
                    {
                        "id": "gpt-bad-limits",
                        "supported_endpoints": ["/responses"],
                        "capabilities": {"limits": 123},
                    },
                ]
            },
        )

    client, _ = _make_client(handler)
    response = client.get("/codex/v1/models")

    assert [model["context_window"] for model in response.json()["models"]] == [272_000, 272_000]
    assert [model["max_context_window"] for model in response.json()["models"]] == [272_000, 272_000]


def test_codex_models_openai_shape_still_present():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-5.5", "vendor": "OpenAI", "supported_endpoints": ["/responses"]}]},
        )

    client, _ = _make_client(handler)
    response = client.get("/codex/v1/models")

    assert response.json()["data"] == [
        {"id": "gpt-5.5", "object": "model", "created": 0, "owned_by": "OpenAI"}
    ]
    assert response.json()["object"] == "list"


def test_codex_models_empty_when_no_responses_models():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "gpt-4o", "supported_endpoints": ["/chat/completions"]}]})

    client, _ = _make_client(handler)
    response = client.get("/codex/v1/models")

    assert response.json() == {
        "object": "list",
        "data": [],
        "models": [],
    }


def test_codex_compact_native_success_passthrough():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://upstream.test/responses/compact"
        return httpx.Response(200, json={"id": "compact_native", "object": "response.compaction"})

    client, _ = _make_client(handler)
    response = client.post(
        "/codex/v1/responses/compact",
        json={"model": "gpt-5.5", "input": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "compact_native"


def test_codex_compact_native_rewrites_body_and_builds_headers():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "compact_native", "object": "response.compaction"})

    client, _ = _make_client(handler)
    response = client.post(
        "/codex/v1/responses/compact",
        json={
            "model": "gpt-5.5",
            "input": [{"role": "assistant", "content": "previous answer"}],
            "previous_response_id": "resp_old",
            "tools": [
                {"type": "image_generation"},
                {"type": "web_search"},
                {"type": "function", "name": "shell"},
            ],
            "tool_choice": {"type": "image_generation"},
        },
        headers={"Authorization": "Bearer local-client-token"},
    )

    assert response.status_code == 200
    assert captured["url"] == "https://upstream.test/responses/compact"
    assert captured["headers"]["authorization"] == "Bearer gho_test"
    assert captured["headers"]["accept"] == "application/json"
    assert captured["headers"]["x-initiator"] == "agent"
    assert captured["headers"]["vscode-sessionid"] == "session-1"
    assert "previous_response_id" not in captured["body"]
    assert captured["body"]["tools"] == [{"type": "web_search"}, {"type": "function", "name": "shell"}]
    assert "tool_choice" not in captured["body"]


def test_codex_compact_native_non_404_error_does_not_fallback():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == "https://upstream.test/responses/compact":
            return httpx.Response(401, content=b"bad token")
        return httpx.Response(200, json={"id": "should_not_call"})

    client, _ = _make_client(handler)
    response = client.post("/codex/v1/responses/compact", json={"model": "gpt-5.5"})

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"
    assert calls == ["https://upstream.test/responses/compact"]


def test_codex_compact_synthetic_fallback_on_404():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), json.loads(request.content)))
        if str(request.url) == "https://upstream.test/responses/compact":
            return httpx.Response(404, content=b"not found")
        assert str(request.url) == "https://upstream.test/responses"
        return httpx.Response(
            200,
            json={
                "id": "resp_summary",
                "object": "response",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "summary"}]}],
                "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
            },
        )

    client, _ = _make_client(handler)
    response = client.post(
        "/codex/v1/responses/compact",
        json={"model": "gpt-5.5", "input": [{"type": "message", "role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "response.compaction"
    assert payload["output"][0]["type"] == "message"
    assert payload["usage"]["total_tokens"] == 5
    assert calls[1][1]["stream"] is False
    assert calls[1][1]["store"] is False
    assert codex_mod.COMPACTION_PROMPT in calls[1][1]["input"][-1]["content"][0]["text"]


def test_codex_healthz_counts_response_models():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-5.5", "supported_endpoints": ["/responses"]},
                    {"id": "gpt-4o", "supported_endpoints": ["/chat/completions"]},
                ]
            },
        )

    client, _ = _make_client(handler)
    response = client.get("/codex/healthz")

    assert response.status_code == 200
    assert response.json()["upstream_ok"] is True
    assert response.json()["response_models"] == 1


def test_codex_healthz_non_json_200_is_not_upstream_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    client, _ = _make_client(handler)
    response = client.get("/codex/healthz")

    assert response.status_code == 200
    assert response.json()["upstream_ok"] is False
    assert response.json()["upstream_status"] == 200
    assert response.json()["response_models"] == 0


def test_openai_status_mapping_covers_codex_error_families():
    assert codex_mod._openai_kind_for_status(403) == "permission_error"
    assert codex_mod._openai_kind_for_status(500) == "server_error"
    assert codex_mod._openai_kind_for_status(418) == "invalid_request_error"


def test_parse_codex_request_handles_malformed_non_object_and_agent_inputs():
    malformed = b"{"
    body, model, streaming, initiator = codex_mod._parse_codex_request(malformed, request_id="rid")
    assert body is malformed
    assert model is None
    assert streaming is False
    assert initiator == "agent"

    non_object = b'["not-an-object"]'
    body, model, streaming, initiator = codex_mod._parse_codex_request(non_object, request_id="rid")
    assert body is non_object
    assert model is None
    assert streaming is False
    assert initiator == "agent"

    assistant_turn = json.dumps({"input": ["ignored", {"role": "assistant"}]}).encode()
    assert codex_mod._parse_codex_request(assistant_turn, request_id="rid")[3] == "agent"

    tool_turn = json.dumps({"input": [{"type": "function_call_output"}]}).encode()
    assert codex_mod._parse_codex_request(tool_turn, request_id="rid")[3] == "agent"


def test_parse_codex_request_removes_empty_tools_and_function_tool_choice():
    raw = json.dumps({
        "tools": [{"type": "image_generation"}],
        "tool_choice": "image_generation",
    }).encode()

    body, _model, _streaming, _initiator = codex_mod._parse_codex_request(raw, request_id="rid")
    parsed = json.loads(body)

    assert "tools" not in parsed
    assert "tool_choice" not in parsed
    assert codex_mod._choice_targets_stripped_tool(
        {"function": {"name": "image_generation"}},
        {"image_generation"},
    )


def test_codex_non_streaming_timeout_and_http_error_are_openai_errors():
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out Authorization: Bearer leaked")

    timeout_client, _ = _make_client(timeout_handler)
    timeout_response = timeout_client.post("/codex/v1/responses", json={"model": "gpt-5.5"})

    assert timeout_response.status_code == 502
    assert timeout_response.json()["error"]["type"] == "server_error"
    assert "leaked" not in timeout_response.text

    def error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connect failed")

    error_client, _ = _make_client(error_handler)
    error_response = error_client.post("/codex/v1/responses", json={"model": "gpt-5.5"})

    assert error_response.status_code == 502
    assert "Upstream error: connect failed" in error_response.json()["error"]["message"]


def test_codex_non_streaming_error_body_read_failure_uses_unavailable_message():
    class BrokenErrorStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError("cannot read error body")
            yield b"unreachable"

        async def aclose(self):
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, stream=BrokenErrorStream())

    client, _ = _make_client(handler)
    response = client.post("/codex/v1/responses", json={"model": "gpt-5.5"})

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "server_error"
    assert "error body unavailable" in response.json()["error"]["message"]


def test_codex_non_streaming_success_body_read_failure_is_openai_error():
    class BrokenSuccessStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError("success body failed Authorization: Bearer leaked")
            yield b"unreachable"

        async def aclose(self):
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=BrokenSuccessStream())

    client, _ = _make_client(handler)
    response = client.post("/codex/v1/responses", json={"model": "gpt-5.5"})

    assert response.status_code == 502
    assert response.headers["content-type"] == "application/json"
    assert response.json()["error"]["type"] == "server_error"
    assert "Upstream error: success body failed" in response.json()["error"]["message"]
    assert "leaked" not in response.text


def test_codex_non_streaming_success_body_timeout_is_openai_error():
    class TimeoutSuccessStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.TimeoutException("success body timed out Authorization: Bearer leaked")
            yield b"unreachable"

        async def aclose(self):
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=TimeoutSuccessStream())

    client, _ = _make_client(handler)
    response = client.post("/codex/v1/responses", json={"model": "gpt-5.5"})

    assert response.status_code == 502
    assert response.headers["content-type"] == "application/json"
    assert response.json()["error"]["type"] == "server_error"
    assert "Upstream timeout: success body timed out" in response.json()["error"]["message"]
    assert "leaked" not in response.text


def test_codex_non_streaming_success_body_non_http_error_is_openai_error(caplog):
    class BrokenSuccessStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"partial":'
            raise OSError("socket failed Authorization: Bearer leaked-codex-oserror-token-12345")

        async def aclose(self):
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=BrokenSuccessStream())

    client, _ = _make_client(handler)
    with caplog.at_level("WARNING"):
        response = client.post("/codex/v1/responses", json={"model": "gpt-5.5"})

    assert response.status_code == 502
    assert response.headers["content-type"] == "application/json"
    payload = response.json()
    assert "type" not in payload
    assert payload["error"]["type"] == "server_error"
    assert "Upstream error: socket failed" in payload["error"]["message"]
    assert "leaked-codex-oserror-token-12345" not in response.text
    log_text = "\n".join(record.message for record in caplog.records)
    assert "leaked-codex-oserror-token-12345" not in log_text


def test_codex_non_streaming_error_suppresses_body_when_disabled(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b'{"secret":"sk-ant-leaked"}')

    client, _ = _make_client(handler, log_bodies=False)
    response = client.post("/codex/v1/responses", json={"model": "gpt-5.5"})

    assert response.status_code == 400
    assert "body suppressed" in caplog.text
    assert "sk-ant-leaked" not in caplog.text


def test_codex_streaming_connect_errors_are_openai_json_errors():
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("stream timeout")

    timeout_client, _ = _make_client(timeout_handler)
    timeout_response = timeout_client.post("/codex/v1/responses", json={"model": "gpt-5.5", "stream": True})

    assert timeout_response.status_code == 502
    assert "Upstream timeout: stream timeout" in timeout_response.json()["error"]["message"]

    def error_handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("socket exploded")

    error_client, _ = _make_client(error_handler)
    error_response = error_client.post("/codex/v1/responses", json={"model": "gpt-5.5", "stream": True})

    assert error_response.status_code == 502
    assert "Upstream stream error: socket exploded" in error_response.json()["error"]["message"]


def test_codex_streaming_pre_error_body_read_failure_uses_unavailable_message():
    class BrokenErrorStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError("cannot read pre-stream error body")
            yield b"unreachable"

        async def aclose(self):
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, stream=BrokenErrorStream())

    client, _ = _make_client(handler)
    response = client.post("/codex/v1/responses", json={"model": "gpt-5.5", "stream": True})

    assert response.status_code == 500
    assert response.json()["error"]["type"] == "server_error"
    assert "error body unavailable" in response.json()["error"]["message"]


def test_codex_streaming_keepalive_and_disconnect_sentinels(monkeypatch):
    async def fake_chunks(upstream, request):
        yield b"", "ping"
        yield b"", "disconnect"

    monkeypatch.setattr(codex_mod, "chunks_with_keepalive", fake_chunks)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b""), headers={"content-type": "text/event-stream"})

    client, _ = _make_client(handler)
    with client.stream("POST", "/codex/v1/responses", json={"model": "gpt-5.5", "stream": True}) as response:
        body = b"".join(response.iter_bytes())

    assert response.status_code == 200
    assert body == b": keepalive\n\n"


def test_codex_compact_rejects_malformed_and_non_object_json():
    client, _ = _make_client(lambda request: httpx.Response(500))

    malformed = client.post("/codex/v1/responses/compact", content=b"{")
    assert malformed.status_code == 400
    assert malformed.json()["error"]["message"] == "Malformed JSON body."

    non_object = client.post("/codex/v1/responses/compact", json=["not-an-object"])
    assert non_object.status_code == 400
    assert non_object.json()["error"]["message"] == "Request body must be a JSON object."


def test_codex_compact_synthetic_requires_model_after_native_404():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://upstream.test/responses/compact"
        return httpx.Response(404, content=b"not found")

    client, _ = _make_client(handler)
    response = client.post("/codex/v1/responses/compact", json={"input": "summarize"})

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Codex compact request requires a model."


def test_codex_compact_synthetic_handles_string_input_and_copies_optional_fields():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), json.loads(request.content)))
        if str(request.url) == "https://upstream.test/responses/compact":
            return httpx.Response(404, content=b"not found")
        return httpx.Response(200, json={"output": [], "usage": {"total_tokens": 1}})

    client, _ = _make_client(handler)
    response = client.post(
        "/codex/v1/responses/compact",
        json={
            "model": "gpt-5.5",
            "input": "raw transcript",
            "instructions": "be brief",
            "reasoning": {"effort": "low"},
            "text": {"verbosity": "low"},
            "metadata": {"a": "b"},
            "client_metadata": {"session": "1"},
            "prompt_cache_key": "cache-key",
        },
    )

    assert response.status_code == 200
    synthetic = calls[1][1]
    assert synthetic["input"][0]["content"][0]["text"] == "raw transcript"
    assert synthetic["instructions"] == "be brief"
    assert synthetic["reasoning"] == {"effort": "low"}
    assert synthetic["text"] == {"verbosity": "low"}
    assert synthetic["metadata"] == {"a": "b"}
    assert synthetic["client_metadata"] == {"session": "1"}
    assert synthetic["prompt_cache_key"] == "cache-key"


def test_codex_compact_synthetic_propagates_upstream_error_and_non_json_success():
    def error_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://upstream.test/responses/compact":
            return httpx.Response(404, content=b"not found")
        return httpx.Response(500, content=b"boom")

    error_client, _ = _make_client(error_handler)
    error_response = error_client.post("/codex/v1/responses/compact", json={"model": "gpt-5.5"})

    assert error_response.status_code == 500
    assert error_response.json()["error"]["type"] == "server_error"

    def non_json_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://upstream.test/responses/compact":
            return httpx.Response(404, content=b"not found")
        return httpx.Response(200, content=b"not json")

    non_json_client, _ = _make_client(non_json_handler)
    non_json_response = non_json_client.post("/codex/v1/responses/compact", json={"model": "gpt-5.5"})

    assert non_json_response.status_code == 502
    assert non_json_response.json()["error"]["message"] == "Synthetic compact upstream returned non-JSON."


def test_codex_models_error_paths_and_duplicate_filtering():
    def connect_error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("models failed Authorization: Bearer leaked-models-token-12345")

    connect_client, _ = _make_client(connect_error_handler)
    connect_response = connect_client.get("/codex/v1/models")
    assert connect_response.status_code == 502
    assert "Upstream /models error: models failed" in connect_response.json()["error"]["message"]
    assert "leaked-models-token-12345" not in connect_response.text

    def status_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"Authorization: Bearer leaked-model-status-token-12345")

    status_client, _ = _make_client(status_handler)
    status_response = status_client.get("/codex/v1/models")
    assert status_response.status_code == 403
    assert status_response.json()["error"]["type"] == "permission_error"
    assert "leaked-model-status-token-12345" not in status_response.text

    def non_json_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    non_json_client, _ = _make_client(non_json_handler)
    non_json_response = non_json_client.get("/codex/v1/models")
    assert non_json_response.status_code == 502
    assert "returned non-JSON body" in non_json_response.json()["error"]["message"]

    def duplicate_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-5.5", "supported_endpoints": ["/responses"]},
                    {"id": "gpt-5.5", "supported_endpoints": ["/responses"]},
                    {"id": "", "supported_endpoints": ["/responses"]},
                    {"supported_endpoints": ["/responses"]},
                ]
            },
        )

    duplicate_client, _ = _make_client(duplicate_handler)
    duplicate_response = duplicate_client.get("/codex/v1/models")
    assert [model["id"] for model in duplicate_response.json()["data"]] == ["gpt-5.5"]


def test_codex_healthz_reports_auth_hint_and_handles_exceptions():
    def auth_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"expired")

    auth_client, _ = _make_client(auth_handler)
    auth_response = auth_client.get("/codex/healthz")

    assert auth_response.status_code == 200
    assert auth_response.json()["upstream_ok"] is False
    assert auth_response.json()["upstream_status"] == 401
    assert "token may be expired" in auth_response.json()["hint"]

    def error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("health failed")

    error_client, _ = _make_client(error_handler)
    error_response = error_client.get("/codex/healthz")

    assert error_response.status_code == 200
    assert error_response.json()["upstream_ok"] is False
    assert error_response.json()["upstream_status"] is None
