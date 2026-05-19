from copilot_cli_relay.headers import (
    _CLAUDE_STRIP_HEADERS,
    build_claude_outbound_headers,
    build_codex_outbound_headers,
)


def test_strips_hop_by_hop_and_inbound_auth():
    inbound = {
        "Authorization": "Bearer leaked",
        "x-api-key": "should-be-removed",
        "Host": "example.com",
        "Content-Length": "123",
        "User-Agent": "claude-cli/1.2",
        "anthropic-beta": "tools-2024-04-04",
        "Accept": "application/json",
    }
    out = build_claude_outbound_headers(
        inbound,
        bearer_token="gho_test",
        integration_id="copilot-developer-cli",
        editor_version="copilot-cli-relay/0.2.0",
        request_id="rid-123",
    )
    # Inbound Authorization replaced, not propagated.
    assert out["Authorization"] == "Bearer gho_test"
    # Inbound User-Agent replaced with our editor identity, not propagated.
    assert out["User-Agent"] == "copilot-cli-relay/0.2.0"
    # Stripped entirely (not re-set by us).
    for h in ("x-api-key", "Host", "Content-Length"):
        assert h not in out
        assert h.lower() not in {k.lower() for k in out}
    assert out["Copilot-Integration-Id"] == "copilot-developer-cli"
    assert out["Editor-Version"] == "copilot-cli-relay/0.2.0"
    assert out["User-Agent"] == "copilot-cli-relay/0.2.0"
    assert out["X-Request-Id"] == "rid-123"
    assert out["anthropic-version"] == "2023-06-01"
    assert out["anthropic-beta"] == "tools-2024-04-04"
    assert out["Accept"] == "application/json"


def test_anthropic_version_passthrough():
    out = build_claude_outbound_headers(
        {"anthropic-version": "2024-10-22"},
        bearer_token="gho_x",
        integration_id="x",
        editor_version="x",
    )
    assert out["anthropic-version"] == "2024-10-22"


def test_strip_set_includes_expected():
    for h in ("connection", "transfer-encoding", "host", "authorization", "x-api-key", "user-agent", "cookie"):
        assert h in _CLAUDE_STRIP_HEADERS


def test_strips_inbound_cookie():
    """Defensive: any client cookies must not ride upstream alongside the
    injected Bearer token."""
    inbound = {"Cookie": "session=abc; csrf=xyz", "X-Trace": "keep"}
    out = build_claude_outbound_headers(
        inbound, bearer_token="t", integration_id="i", editor_version="e/1",
    )
    assert "Cookie" not in out
    assert "cookie" not in {k.lower() for k in out}
    assert out.get("X-Trace") == "keep"


def test_strips_connection_named_dynamic_hop_by_hop():
    """RFC 7230 §6.1: header names listed in `Connection` are also per-hop.
    They must be stripped from the outbound request."""
    inbound = {
        "Connection": "close, X-Custom-Hop, X-Another-Hop",
        "X-Custom-Hop": "leak-me",
        "X-Another-Hop": "leak-too",
        "X-Keep": "ok",
    }
    out = build_claude_outbound_headers(
        inbound, bearer_token="t", integration_id="i", editor_version="e/1",
    )
    keys = {k.lower() for k in out}
    assert "x-custom-hop" not in keys
    assert "x-another-hop" not in keys
    assert "connection" not in keys  # Connection itself is hop-by-hop
    assert out.get("X-Keep") == "ok"


def test_strips_trailer_singular_inbound():
    """RFC 7230 §6.1 hop-by-hop header is `Trailer` (singular). Must not leak upstream."""
    out = build_claude_outbound_headers(
        {"Trailer": "X-Foo", "Trailers": "X-Bar"},
        bearer_token="gho_x",
        integration_id="x",
        editor_version="x",
    )
    keys = {k.lower() for k in out}
    assert "trailer" not in keys
    assert "trailers" not in keys


def test_build_codex_headers_rebuilds_from_scratch():
    out = build_codex_outbound_headers(
        {
            "Authorization": "Bearer leaked",
            "x-api-key": "sk-leaked",
            "OpenAI-Organization": "org-leaked",
            "Cookie": "session=leaked",
            "x-codex-turn-metadata": "local",
            "Accept": "application/json",
        },
        bearer_token="gho_test",
        integration_id="copilot-developer-cli",
        editor_version="vscode/1.99.0",
        plugin_version="copilot-chat/0.43.2026033101",
        user_agent="GitHubCopilotChat/0.43.2026033101",
        github_api_version="2026-01-09",
        session_id="session-1",
        machine_id="a" * 64,
        request_id="rid",
        initiator="agent",
        accept="text/event-stream",
    )
    keys = {k.lower() for k in out}
    assert out["Authorization"] == "Bearer gho_test"
    assert out["Accept"] == "text/event-stream"
    assert out["Copilot-Integration-Id"] == "copilot-developer-cli"
    assert out["Editor-Version"] == "vscode/1.99.0"
    assert out["Editor-Plugin-Version"] == "copilot-chat/0.43.2026033101"
    assert out["User-Agent"] == "GitHubCopilotChat/0.43.2026033101"
    assert out["OpenAI-Intent"] == "conversation-panel"
    assert out["X-Interaction-Type"] == "conversation-panel"
    assert out["X-GitHub-Api-Version"] == "2026-01-09"
    assert out["VScode-SessionId"] == "session-1"
    assert out["VScode-MachineId"] == "a" * 64
    assert out["X-Initiator"] == "agent"
    assert out["X-Request-Id"] == "rid"
    assert "x-api-key" not in keys
    assert "openai-organization" not in keys
    assert "cookie" not in keys
    assert "x-codex-turn-metadata" not in keys
