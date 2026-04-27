import json

from claude_copilot_cli_relay.errors import envelope, json_error, sse_error_event


def test_envelope_shape():
    e = envelope("rate_limit_error", "slow down")
    assert e == {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}


def test_json_error_status_defaults():
    r = json_error("authentication_error", "bad token")
    assert r.status_code == 401


def test_json_error_status_override():
    r = json_error("api_error", "boom", status=502)
    assert r.status_code == 502


def test_sse_error_event_format():
    chunk = sse_error_event("api_error", "boom")
    text = chunk.decode()
    assert text.startswith("event: error\n")
    assert text.endswith("\n\n")
    payload = text.split("data: ", 1)[1].strip()
    obj = json.loads(payload)
    assert obj["error"]["type"] == "api_error"
