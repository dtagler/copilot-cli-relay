import json

from copilot_cli_relay.claude_proxy import _parse_claude_request


def _rt(body: dict) -> dict:
    out, _model, _stream = _parse_claude_request(json.dumps(body).encode())
    return json.loads(out)


def test_xhigh_clamped_to_high_for_default_models():
    out = _rt({"model": "claude-sonnet-4.6", "output_config": {"effort": "xhigh"}})
    assert out["output_config"]["effort"] == "high"


def test_opus_47_clamps_high_to_medium():
    out = _rt({"model": "claude-opus-4.7", "output_config": {"effort": "high"}})
    assert out["output_config"]["effort"] == "medium"


def test_opus_47_clamps_xhigh_to_medium():
    out = _rt({"model": "claude-opus-4.7", "reasoning_effort": "xhigh"})
    assert out["reasoning_effort"] == "medium"


def test_haiku_strips_effort_entirely():
    out = _rt({
        "model": "claude-haiku-4.5",
        "reasoning_effort": "high",
        "output_config": {"effort": "high"},
    })
    assert "reasoning_effort" not in out
    assert "output_config" not in out


def test_no_effort_field_is_passthrough():
    body = {"model": "claude-sonnet-4.6", "messages": [{"role": "user", "content": "hi"}]}
    out_bytes, model, stream = _parse_claude_request(json.dumps(body).encode())
    assert json.loads(out_bytes) == body
    assert model == "claude-sonnet-4.6"
    assert stream is False


def test_already_valid_passthrough():
    out = _rt({"model": "claude-sonnet-4.6", "output_config": {"effort": "medium"}})
    assert out["output_config"]["effort"] == "medium"


def test_malformed_body_passthrough():
    raw = b"not json{"
    out, model, stream = _parse_claude_request(raw)
    assert out == raw
    assert model is None
    assert stream is False


def test_non_string_effort_passes_through_unchanged():
    """Future API expansion (e.g. dict shapes) must not be silently coerced
    to a string — pass through and let upstream return its own error."""
    body = {
        "model": "claude-sonnet-4.6",
        "reasoning_effort": {"level": "high", "tokens": 1000},
        "output_config": {"effort": 5},
    }
    out, _, _ = _parse_claude_request(json.dumps(body).encode())
    obj = json.loads(out)
    # Original shapes preserved
    assert obj["reasoning_effort"] == {"level": "high", "tokens": 1000}
    assert obj["output_config"]["effort"] == 5


def test_haiku_strips_non_string_effort_too():
    """Haiku doesn't support reasoning_effort at all — strip regardless of
    the value's type so we never send an invalid shape."""
    body = {
        "model": "claude-haiku-4.5",
        "reasoning_effort": {"level": "high"},
        "output_config": {"effort": 7, "other": 1},
    }
    out, _, _ = _parse_claude_request(json.dumps(body).encode())
    obj = json.loads(out)
    assert "reasoning_effort" not in obj
    assert obj["output_config"] == {"other": 1}
