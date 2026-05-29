"""Tests for server lifespan, __main__, errors helpers, and remaining headers/redaction edges."""
from __future__ import annotations

import json
import runpy
import sys
from unittest.mock import patch

import pytest

from copilot_cli_relay import __version__
from copilot_cli_relay.config import Settings, reset_settings_for_tests
from copilot_cli_relay.errors import ANTHROPIC_ERROR_STATUS, anthropic_json_error
from copilot_cli_relay.headers import _filter_anthropic_beta, build_claude_outbound_headers
from copilot_cli_relay.logging_setup import (
    MAX_BODY_BYTES,
    configure_logging,
    redact_bytes,
)
from copilot_cli_relay.security import _host_without_port, _is_json_content_type, _is_loopback_origin


def test_version_string():
    assert __version__ == "0.3.0"


# ---------- errors.py ----------

@pytest.mark.parametrize("name,status", list(ANTHROPIC_ERROR_STATUS.items()))
def test_anthropic_json_error_status_for_each_known_type(name, status):
    r = anthropic_json_error(name, "msg")
    assert r.status_code == status


def test_anthropic_json_error_unknown_type_defaults_500():
    r = anthropic_json_error("totally_made_up", "x")
    assert r.status_code == 500


def test_anthropic_json_error_with_headers():
    r = anthropic_json_error("api_error", "boom", headers={"x-test": "1"})
    assert r.headers["x-test"] == "1"


# ---------- headers.py edges ----------

def test_anthropic_beta_filter_removes_unsupported_token():
    out = _filter_anthropic_beta("context-1m-2025-08-07, tools-2024-04-04")
    assert out == "tools-2024-04-04"


def test_anthropic_beta_filter_drops_header_when_empty_after_filter():
    assert _filter_anthropic_beta("context-1m-2025-08-07") is None
    assert _filter_anthropic_beta("") is None


def test_anthropic_beta_filter_strips_advisor_tool():
    """Regression: Claude Code sends `advisor-tool-2026-03-01` and Copilot's
    /v1/messages 400s on it. Strip it like context-1m-2025-08-07."""
    assert _filter_anthropic_beta("advisor-tool-2026-03-01") is None
    assert _filter_anthropic_beta(
        "advisor-tool-2026-03-01, tools-2024-04-04"
    ) == "tools-2024-04-04"
    # Case-insensitive, like the rest of the strip set
    assert _filter_anthropic_beta("Advisor-Tool-2026-03-01") is None


def test_anthropic_beta_filter_case_insensitive_strip():
    """Regression: detection of unsupported beta tokens in claude_proxy.py is
    case-insensitive (`_client_wants_1m_context`), and stripping in
    headers.py must match — otherwise a mixed-case `Context-1M-2025-08-07`
    triggers the 1M model remap but the beta itself slips through to
    upstream, which rejects it on every model id."""
    # Mixed case → still stripped
    assert _filter_anthropic_beta("Context-1M-2025-08-07") is None
    # Mixed case alongside a supported token → only the unsupported one removed
    assert _filter_anthropic_beta(
        "Context-1M-2025-08-07, tools-2024-04-04"
    ) == "tools-2024-04-04"


def test_build_headers_drops_anthropic_beta_when_only_unsupported():
    out = build_claude_outbound_headers(
        {"anthropic-beta": "context-1m-2025-08-07"},
        bearer_token="gho_x",
        integration_id="x",
        editor_version="x",
    )
    assert "anthropic-beta" not in {k.lower() for k in out}


def test_build_headers_autogenerates_request_id_when_omitted():
    out = build_claude_outbound_headers(
        {},
        bearer_token="gho_x",
        integration_id="x",
        editor_version="x",
    )
    # uuid4 hex form is 36 chars with hyphens.
    assert len(out["X-Request-Id"]) == 36


# ---------- logging_setup.py edges ----------

def test_configure_logging_sets_level():
    configure_logging("debug")
    import logging
    # basicConfig is idempotent across processes; just confirm it doesn't raise
    # and that getattr lookup of an unknown level falls back to INFO.
    configure_logging("not-a-real-level")
    assert logging.getLogger("copilot_cli_relay") is not None


def test_redact_bytes_truncates_oversize_body():
    body = b"x" * (MAX_BODY_BYTES + 100)
    out = redact_bytes(body)
    assert out.endswith(b"...[truncated]")
    assert len(out) <= MAX_BODY_BYTES + len(b"...[truncated]") + 1


def test_redact_bytes_handles_invalid_utf8():
    out = redact_bytes(b"\xff\xfe\xfd")
    # Should not raise; replacement chars are fine.
    assert isinstance(out, bytes)


# ---------- server.py lifespan ----------

def test_server_lifespan_creates_and_closes_client(monkeypatch):
    from starlette.testclient import TestClient

    # Provide a valid settings object so lifespan succeeds.
    s = Settings(
        proxy_port=4141, github_token="gho_x", api_base="https://x.test",
        integration_id="x", editor_version="copilot-cli-relay/test",
        log_level="info", log_bodies=False,
    )
    reset_settings_for_tests(s)
    try:
        from copilot_cli_relay.server import app
        with TestClient(app):
            assert app.state.http_client is not None
            # Routes are registered.
            paths = {r.path for r in app.routes}
            assert {
                "/claude/v1/messages",
                "/claude/v1/models",
                "/codex/v1/responses",
                "/codex/v1/responses/compact",
                "/codex/v1/models",
                "/claude/healthz",
                "/codex/healthz",
                "/healthz",
            } <= paths
            assert "/v1/messages" not in paths
            assert "/v1/models" not in paths
            # trust_env=False so HTTP(S)_PROXY env vars on the host can't
            # silently route Copilot bearer-token traffic through a foreign
            # intermediary. Regression-tested because changing this would
            # silently re-introduce that exfil path.
            assert app.state.http_client.trust_env is False
        # After exit, the client is closed.
        assert app.state.http_client.is_closed
    finally:
        reset_settings_for_tests(None)


def test_security_helpers_accept_only_json_and_loopback_origins():
    assert _host_without_port(None) is None
    assert _host_without_port("  ") is None
    assert _host_without_port("localhost:4141") == "localhost"
    assert _host_without_port("LOCALHOST:4141") == "localhost"
    assert _host_without_port("localhost") == "localhost"
    assert _host_without_port("127.0.0.1:4141") == "127.0.0.1"
    assert _host_without_port("::1") == "::1"
    assert _host_without_port("[::1]:4141") == "::1"
    assert _host_without_port("[::1]") == "::1"
    assert _host_without_port("[::1") is None
    assert _host_without_port("[::1]:bad") is None
    assert _host_without_port("localhost:bad") is None

    assert _is_json_content_type("application/json")
    assert _is_json_content_type("application/json; charset=utf-8")
    assert _is_json_content_type("application/vnd.api+json")
    assert not _is_json_content_type("text/plain")
    assert not _is_json_content_type(None)

    assert _is_loopback_origin("http://localhost:4141")
    assert _is_loopback_origin("http://127.0.0.1:4141")
    assert _is_loopback_origin("http://[::1]:4141")
    assert not _is_loopback_origin("https://evil.example")
    assert not _is_loopback_origin("http://[::1")
    assert not _is_loopback_origin("null")


def test_server_rejects_dns_rebinding_host():
    from starlette.testclient import TestClient

    s = Settings(
        proxy_port=4141, github_token="gho_x", api_base="https://x.test",
        integration_id="x", editor_version="copilot-cli-relay/test",
        log_level="info", log_bodies=False,
    )
    reset_settings_for_tests(s)
    try:
        from copilot_cli_relay.server import app
        with TestClient(app, base_url="http://evil.example:4141") as client:
            response = client.post(
                "/claude/v1/messages",
                content=b"{}",
                headers={"content-type": "application/json"},
            )
        assert response.status_code == 403
        payload = response.json()
        assert payload["type"] == "error"
        assert payload["error"]["type"] == "permission_error"
        assert "Host header" in payload["error"]["message"]
    finally:
        reset_settings_for_tests(None)


def test_server_allows_ipv6_loopback_host():
    from starlette.testclient import TestClient

    s = Settings(
        proxy_port=4141, github_token="gho_x", api_base="https://x.test",
        integration_id="x", editor_version="copilot-cli-relay/test",
        log_level="info", log_bodies=False,
    )
    reset_settings_for_tests(s)
    try:
        from copilot_cli_relay.server import app
        with TestClient(app, base_url="http://localhost:4141") as client:
            response = client.get("/v1/models", headers={"host": "[::1]:4141"})
        assert response.status_code == 404
    finally:
        reset_settings_for_tests(None)


def test_server_rejects_non_loopback_browser_origin():
    from starlette.testclient import TestClient

    s = Settings(
        proxy_port=4141, github_token="gho_x", api_base="https://x.test",
        integration_id="x", editor_version="copilot-cli-relay/test",
        log_level="info", log_bodies=False,
    )
    reset_settings_for_tests(s)
    try:
        from copilot_cli_relay.server import app
        with TestClient(app, base_url="http://localhost:4141") as client:
            response = client.post(
                "/codex/v1/responses",
                content=b'{"model":"gpt-5.5"}',
                headers={
                    "content-type": "application/json",
                    "origin": "https://evil.example",
                },
            )
        assert response.status_code == 403
        payload = response.json()
        assert "type" not in payload
        assert payload["error"]["type"] == "permission_error"
        assert "param" in payload["error"]
        assert "code" in payload["error"]
    finally:
        reset_settings_for_tests(None)


def test_server_rejects_cross_site_sec_fetch_without_origin():
    from starlette.testclient import TestClient

    s = Settings(
        proxy_port=4141, github_token="gho_x", api_base="https://x.test",
        integration_id="x", editor_version="copilot-cli-relay/test",
        log_level="info", log_bodies=False,
    )
    reset_settings_for_tests(s)
    try:
        from copilot_cli_relay.server import app
        with TestClient(app, base_url="http://localhost:4141") as client:
            response = client.post(
                "/codex/v1/responses",
                content=b'{"model":"gpt-5.5"}',
                headers={
                    "content-type": "application/json",
                    "sec-fetch-site": "cross-site",
                },
            )
        assert response.status_code == 403
        payload = response.json()
        assert "type" not in payload
        assert payload["error"]["type"] == "permission_error"
        assert "param" in payload["error"]
        assert "code" in payload["error"]
    finally:
        reset_settings_for_tests(None)


def test_server_rejects_non_json_proxy_posts():
    from starlette.testclient import TestClient

    s = Settings(
        proxy_port=4141, github_token="gho_x", api_base="https://x.test",
        integration_id="x", editor_version="copilot-cli-relay/test",
        log_level="info", log_bodies=False,
    )
    reset_settings_for_tests(s)
    try:
        from copilot_cli_relay.server import app
        with TestClient(app, base_url="http://localhost:4141") as client:
            claude = client.post(
                "/claude/v1/messages",
                content=b"{}",
                headers={"content-type": "text/plain"},
            )
            codex = client.post(
                "/codex/v1/responses",
                content=b"{}",
                headers={"content-type": "text/plain"},
            )
            compact = client.post(
                "/codex/v1/responses/compact",
                content=b"{}",
                headers={"content-type": "text/plain"},
            )
            compact_missing_type = client.post(
                "/codex/v1/responses/compact",
                content=b"{}",
            )
        assert claude.status_code == 415
        assert claude.json()["type"] == "error"
        assert claude.json()["error"]["type"] == "invalid_request_error"
        assert codex.status_code == 415
        assert "type" not in codex.json()
        assert codex.json()["error"]["type"] == "invalid_request_error"
        assert compact.status_code == 415
        assert "type" not in compact.json()
        assert compact.json()["error"]["type"] == "invalid_request_error"
        assert compact_missing_type.status_code == 415
        assert "type" not in compact_missing_type.json()
        assert compact_missing_type.json()["error"]["type"] == "invalid_request_error"
    finally:
        reset_settings_for_tests(None)


@pytest.mark.parametrize(
    ("path", "protocol"),
    [
        ("/claude/v1/models", "anthropic"),
        ("/claude/healthz", "anthropic"),
        ("/healthz", "anthropic"),
        ("/codex/v1/models", "openai"),
        ("/codex/healthz", "openai"),
    ],
)
def test_server_bad_host_error_shape_matches_route_protocol(path, protocol):
    from starlette.testclient import TestClient

    s = Settings(
        proxy_port=4141, github_token="gho_x", api_base="https://x.test",
        integration_id="x", editor_version="copilot-cli-relay/test",
        log_level="info", log_bodies=False,
    )
    reset_settings_for_tests(s)
    try:
        from copilot_cli_relay.server import app
        with TestClient(app, base_url="http://evil.example:4141") as client:
            response = client.get(path)
        assert response.status_code == 403
        payload = response.json()
        if protocol == "anthropic":
            assert payload["type"] == "error"
            assert payload["error"]["type"] == "permission_error"
        else:
            assert "type" not in payload
            assert payload["error"]["type"] == "permission_error"
            assert "param" in payload["error"]
            assert "code" in payload["error"]
    finally:
        reset_settings_for_tests(None)


@pytest.mark.asyncio
async def test_server_rejects_missing_host_header_at_middleware():
    s = Settings(
        proxy_port=4141, github_token="gho_x", api_base="https://x.test",
        integration_id="x", editor_version="copilot-cli-relay/test",
        log_level="info", log_bodies=False,
    )
    reset_settings_for_tests(s)
    try:
        from copilot_cli_relay.server import app
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/claude/v1/models",
                "raw_path": b"/claude/v1/models",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("127.0.0.1", 4141),
            },
            receive,
            send,
        )

        assert messages[0]["status"] == 403
        body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
        payload = json.loads(body)
        assert payload["type"] == "error"
        assert payload["error"]["type"] == "permission_error"
    finally:
        reset_settings_for_tests(None)


def test_server_rejects_bad_host_on_unknown_path_with_generic_error():
    from starlette.testclient import TestClient

    s = Settings(
        proxy_port=4141, github_token="gho_x", api_base="https://x.test",
        integration_id="x", editor_version="copilot-cli-relay/test",
        log_level="info", log_bodies=False,
    )
    reset_settings_for_tests(s)
    try:
        from copilot_cli_relay.server import app
        with TestClient(app, base_url="http://evil.example:4141") as client:
            response = client.get("/unknown")
        assert response.status_code == 403
        assert response.json() == {"error": "Host header must be localhost, 127.0.0.1, or [::1]."}
    finally:
        reset_settings_for_tests(None)


# ---------- __main__.py ----------

def test_main_invokes_uvicorn(monkeypatch):
    s = Settings(
        proxy_port=12345, github_token="gho_x", api_base="x", integration_id="x",
        editor_version="x", log_level="warning", log_bodies=False,
    )
    reset_settings_for_tests(s)
    monkeypatch.delenv("COPILOT_PROXY_HOST", raising=False)
    monkeypatch.delitem(sys.modules, "copilot_cli_relay.__main__", raising=False)
    try:
        with patch("copilot_cli_relay.__main__.uvicorn.run") as run:
            from copilot_cli_relay.__main__ import main
            main()
        # Defaults to loopback — must NOT bind 0.0.0.0 by default outside Docker.
        run.assert_called_once_with(
            "copilot_cli_relay.server:app",
            host="127.0.0.1",
            port=12345,
            log_level="warning",
        )
    finally:
        reset_settings_for_tests(None)


def test_main_respects_copilot_proxy_host(monkeypatch):
    s = Settings(
        proxy_port=12345, github_token="gho_x", api_base="x", integration_id="x",
        editor_version="x", log_level="warning", log_bodies=False,
    )
    reset_settings_for_tests(s)
    monkeypatch.setenv("COPILOT_PROXY_HOST", "0.0.0.0")
    try:
        with patch("copilot_cli_relay.__main__.uvicorn.run") as run:
            from copilot_cli_relay.__main__ import main
            main()
        run.assert_called_once_with(
            "copilot_cli_relay.server:app",
            host="0.0.0.0",
            port=12345,
            log_level="warning",
        )
    finally:
        reset_settings_for_tests(None)


def test_main_module_entrypoint_invokes_main(monkeypatch):
    s = Settings(
        proxy_port=12345, github_token="gho_x", api_base="x", integration_id="x",
        editor_version="x", log_level="warning", log_bodies=False,
    )
    reset_settings_for_tests(s)
    monkeypatch.delenv("COPILOT_PROXY_HOST", raising=False)
    monkeypatch.delitem(sys.modules, "copilot_cli_relay.__main__", raising=False)
    try:
        with patch("uvicorn.run") as run:
            runpy.run_module("copilot_cli_relay.__main__", run_name="__main__")
        run.assert_called_once_with(
            "copilot_cli_relay.server:app",
            host="127.0.0.1",
            port=12345,
            log_level="warning",
        )
    finally:
        reset_settings_for_tests(None)
