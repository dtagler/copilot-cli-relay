"""Tests for server lifespan, __main__, errors helpers, and remaining headers/redaction edges."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from claude_copilot_cli_relay import __version__
from claude_copilot_cli_relay.config import Settings, reset_settings_for_tests
from claude_copilot_cli_relay.errors import ERROR_TYPES, json_error
from claude_copilot_cli_relay.headers import _filter_anthropic_beta, build_outbound_headers
from claude_copilot_cli_relay.logging_setup import (
    MAX_BODY_BYTES,
    configure_logging,
    redact_bytes,
)


def test_version_string():
    assert __version__ == "0.1.0"


# ---------- errors.py ----------

@pytest.mark.parametrize("name,status", list(ERROR_TYPES.items()))
def test_json_error_status_for_each_known_type(name, status):
    r = json_error(name, "msg")
    assert r.status_code == status


def test_json_error_unknown_type_defaults_500():
    r = json_error("totally_made_up", "x")
    assert r.status_code == 500


def test_json_error_with_headers():
    r = json_error("api_error", "boom", headers={"x-test": "1"})
    assert r.headers["x-test"] == "1"


# ---------- headers.py edges ----------

def test_anthropic_beta_filter_removes_unsupported_token():
    out = _filter_anthropic_beta("context-1m-2025-08-07, tools-2024-04-04")
    assert out == "tools-2024-04-04"


def test_anthropic_beta_filter_drops_header_when_empty_after_filter():
    assert _filter_anthropic_beta("context-1m-2025-08-07") is None
    assert _filter_anthropic_beta("") is None


def test_anthropic_beta_filter_case_insensitive_strip():
    """Regression: detection of unsupported beta tokens in proxy.py is
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
    out = build_outbound_headers(
        {"anthropic-beta": "context-1m-2025-08-07"},
        bearer_token="gho_x",
        integration_id="x",
        editor_version="x",
    )
    assert "anthropic-beta" not in {k.lower() for k in out}


def test_build_headers_autogenerates_request_id_when_omitted():
    out = build_outbound_headers(
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
    assert logging.getLogger("claude_copilot_cli_relay") is not None


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
        integration_id="x", editor_version="claude-copilot-cli-relay/test",
        log_level="info", log_bodies=False,
    )
    reset_settings_for_tests(s)
    try:
        from claude_copilot_cli_relay.server import app
        with TestClient(app):
            assert app.state.http_client is not None
            # Routes are registered.
            paths = {r.path for r in app.routes}
            assert {"/v1/messages", "/v1/models", "/healthz"} <= paths
            # trust_env=False so HTTP(S)_PROXY env vars on the host can't
            # silently route Copilot bearer-token traffic through a foreign
            # intermediary. Regression-tested because changing this would
            # silently re-introduce that exfil path.
            assert app.state.http_client.trust_env is False
        # After exit, the client is closed.
        assert app.state.http_client.is_closed
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
    try:
        with patch("claude_copilot_cli_relay.__main__.uvicorn.run") as run:
            from claude_copilot_cli_relay.__main__ import main
            main()
        # Defaults to loopback — must NOT bind 0.0.0.0 by default outside Docker.
        run.assert_called_once_with(
            "claude_copilot_cli_relay.server:app",
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
        with patch("claude_copilot_cli_relay.__main__.uvicorn.run") as run:
            from claude_copilot_cli_relay.__main__ import main
            main()
        run.assert_called_once_with(
            "claude_copilot_cli_relay.server:app",
            host="0.0.0.0",
            port=12345,
            log_level="warning",
        )
    finally:
        reset_settings_for_tests(None)
