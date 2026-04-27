from claude_copilot_cli_relay.headers import _STRIP_HEADERS, build_outbound_headers


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
    out = build_outbound_headers(
        inbound,
        bearer_token="gho_test",
        integration_id="copilot-developer-cli",
        editor_version="claude-copilot-cli-relay/0.1.0",
        request_id="rid-123",
    )
    # Inbound Authorization replaced, not propagated.
    assert out["Authorization"] == "Bearer gho_test"
    # Inbound User-Agent replaced with our editor identity, not propagated.
    assert out["User-Agent"] == "claude-copilot-cli-relay/0.1.0"
    # Stripped entirely (not re-set by us).
    for h in ("x-api-key", "Host", "Content-Length"):
        assert h not in out
        assert h.lower() not in {k.lower() for k in out}
    assert out["Copilot-Integration-Id"] == "copilot-developer-cli"
    assert out["Editor-Version"] == "claude-copilot-cli-relay/0.1.0"
    assert out["User-Agent"] == "claude-copilot-cli-relay/0.1.0"
    assert out["X-Request-Id"] == "rid-123"
    assert out["anthropic-version"] == "2023-06-01"
    assert out["anthropic-beta"] == "tools-2024-04-04"
    assert out["Accept"] == "application/json"


def test_anthropic_version_passthrough():
    out = build_outbound_headers(
        {"anthropic-version": "2024-10-22"},
        bearer_token="gho_x",
        integration_id="x",
        editor_version="x",
    )
    assert out["anthropic-version"] == "2024-10-22"


def test_strip_set_includes_expected():
    for h in ("connection", "transfer-encoding", "host", "authorization", "x-api-key", "user-agent", "cookie"):
        assert h in _STRIP_HEADERS


def test_strips_inbound_cookie():
    """Defensive: any client cookies must not ride upstream alongside the
    injected Bearer token."""
    inbound = {"Cookie": "session=abc; csrf=xyz", "X-Trace": "keep"}
    out = build_outbound_headers(
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
    out = build_outbound_headers(
        inbound, bearer_token="t", integration_id="i", editor_version="e/1",
    )
    keys = {k.lower() for k in out}
    assert "x-custom-hop" not in keys
    assert "x-another-hop" not in keys
    assert "connection" not in keys  # Connection itself is hop-by-hop
    assert out.get("X-Keep") == "ok"


def test_strips_trailer_singular_inbound():
    """RFC 7230 §6.1 hop-by-hop header is `Trailer` (singular). Must not leak upstream."""
    out = build_outbound_headers(
        {"Trailer": "X-Foo", "Trailers": "X-Bar"},
        bearer_token="gho_x",
        integration_id="x",
        editor_version="x",
    )
    keys = {k.lower() for k in out}
    assert "trailer" not in keys
    assert "trailers" not in keys
