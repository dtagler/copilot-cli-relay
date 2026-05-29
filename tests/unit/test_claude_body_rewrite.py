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


def test_opus_47_relocates_and_clamps_top_level_reasoning_effort():
    # Top-level reasoning_effort is no longer accepted upstream; it's relocated
    # into output_config.effort and clamped to the model's accepted set.
    out = _rt({"model": "claude-opus-4.7", "reasoning_effort": "xhigh"})
    assert "reasoning_effort" not in out
    assert out["output_config"]["effort"] == "medium"


def test_opus_48_clamps_high_to_medium():
    # Opus 4.8 advertises reasoning_effort == ["medium"] upstream, same as 4.7.
    out = _rt({"model": "claude-opus-4.8", "output_config": {"effort": "high"}})
    assert out["output_config"]["effort"] == "medium"


def test_opus_48_dash_form_relocates_and_clamps_low_to_medium():
    out = _rt({"model": "claude-opus-4-8", "reasoning_effort": "low"})
    assert "reasoning_effort" not in out
    assert out["output_config"]["effort"] == "medium"


def test_top_level_reasoning_effort_relocated_to_output_config():
    # No clamp needed (value already allowed) but the field still moves.
    out = _rt({"model": "claude-sonnet-4.6", "reasoning_effort": "high"})
    assert "reasoning_effort" not in out
    assert out["output_config"]["effort"] == "high"


def test_existing_output_config_effort_wins_over_top_level():
    out = _rt({
        "model": "claude-sonnet-4.6",
        "reasoning_effort": "low",
        "output_config": {"effort": "high"},
    })
    assert "reasoning_effort" not in out
    assert out["output_config"]["effort"] == "high"


def test_thinking_enabled_rewritten_to_adaptive():
    out = _rt({
        "model": "claude-opus-4.8",
        "thinking": {"type": "enabled", "budget_tokens": 2048},
    })
    assert out["thinking"] == {"type": "adaptive"}


def test_thinking_adaptive_budget_tokens_stripped():
    # Upstream rejects budget_tokens under adaptive too, so strip it even when
    # the client already sent type=adaptive.
    out = _rt({
        "model": "claude-opus-4.8",
        "thinking": {"type": "adaptive", "budget_tokens": 2048},
    })
    assert out["thinking"] == {"type": "adaptive"}


def test_thinking_adaptive_without_budget_passthrough():
    body = {"model": "claude-opus-4.8", "thinking": {"type": "adaptive"}, "max_tokens": 8}
    out_bytes, _, _ = _parse_claude_request(json.dumps(body).encode())
    assert json.loads(out_bytes) == body  # no mutation -> original bytes


def test_thinking_other_types_pass_through():
    out = _rt({"model": "claude-opus-4.8", "thinking": {"type": "disabled"}})
    assert out["thinking"] == {"type": "disabled"}


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


def test_non_string_top_level_reasoning_effort_dropped():
    """Top-level reasoning_effort is no longer an accepted upstream field, so a
    non-string value (which can't sensibly become output_config.effort) is
    dropped rather than forwarded into a guaranteed 400. A non-string
    output_config.effort still passes through for upstream to judge."""
    body = {
        "model": "claude-sonnet-4.6",
        "reasoning_effort": {"level": "high", "tokens": 1000},
        "output_config": {"effort": 5},
    }
    out, _, _ = _parse_claude_request(json.dumps(body).encode())
    obj = json.loads(out)
    assert "reasoning_effort" not in obj
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
