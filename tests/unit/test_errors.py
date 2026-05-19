import json

from copilot_cli_relay.errors import (
    anthropic_envelope,
    anthropic_json_error,
    anthropic_sse_error_event,
    openai_json_error,
    openai_sse_error_event,
)


def test_anthropic_envelope_shape():
    e = anthropic_envelope("rate_limit_error", "slow down")
    assert e == {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}


def test_anthropic_json_error_status_defaults():
    r = anthropic_json_error("authentication_error", "bad token")
    assert r.status_code == 401


def test_anthropic_json_error_status_override():
    r = anthropic_json_error("api_error", "boom", status=502)
    assert r.status_code == 502


def test_anthropic_sse_error_event_format():
    chunk = anthropic_sse_error_event("api_error", "boom")
    text = chunk.decode()
    assert text.startswith("event: error\n")
    assert text.endswith("\n\n")
    payload = text.split("data: ", 1)[1].strip()
    obj = json.loads(payload)
    assert obj["error"]["type"] == "api_error"


def test_openai_json_error_shape():
    response = openai_json_error(
        "invalid_request_error",
        "bad field",
        status=400,
        code="unsupported_value",
        param="tools",
    )
    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload == {
        "error": {
            "message": "bad field",
            "type": "invalid_request_error",
            "param": "tools",
            "code": "unsupported_value",
        }
    }


def test_openai_sse_error_event_format():
    chunk = openai_sse_error_event("server_error", "boom")
    text = chunk.decode()
    assert text.startswith("event: error\n")
    assert text.endswith("\n\n")
    payload = text.split("data: ", 1)[1].strip()
    obj = json.loads(payload)
    assert obj["type"] == "error"
    assert obj["code"] == "server_error"
    assert obj["message"] == "boom"
    assert "error" not in obj
