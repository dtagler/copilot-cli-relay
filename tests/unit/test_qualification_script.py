"""Offline tests for opt-in qualification; no model-generated commands execute."""
from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path

import anyio
import httpx
import pytest

_path = Path(__file__).resolve().parents[2] / "scripts/qualify-responses.py"
_spec = importlib.util.spec_from_file_location("qualify_responses", _path)
qualifier = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qualifier)


@pytest.mark.parametrize("base", [
    "https://external.example/codex/v1", "http://user:secret@127.0.0.1/codex/v1",
    "http://127.0.0.1/codex/v1?secret=x", "http://127.0.0.1/wrong",
])
def test_qualification_rejects_unsafe_destinations(base):
    with pytest.raises(ValueError):
        qualifier.validate_base(base)


def test_qualification_plan_never_constructs_http_client(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["qualify-responses.py", "--model", "fixture"])
    monkeypatch.setattr(qualifier.httpx, "AsyncClient", lambda **_kw: pytest.fail("Plan attempted network access."))
    assert qualifier.main() == 0
    assert "Plan only" in capsys.readouterr().out


def sse(output, status="completed"):
    event = {"type": "response.completed", "response": {"status": status, "output": output}}
    return f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode()


def message(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


async def test_synthetic_qualification_preserves_tool_identity_and_replay():
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        index = len(calls)
        assert payload["model"] == "fixture"
        assert payload["max_output_tokens"] <= 512
        if index == 1:
            output = [message("relay-qualification-ok")]
        elif index == 2:
            output = [{"type": "function_call", "name": "read_fixture", "call_id": "fixture-call", "arguments": "{}"}]
        elif index in (3, 4):
            assert any(item.get("type") == "function_call_output" and item["call_id"] == "fixture-call"
                       for item in payload["input"])
            output = [message("fixture-marker-739")]
        elif index == 5:
            return httpx.Response(200, content=b'data: {"type":"response.output_text.delta","delta":"1"}\n\n')
        else:
            output = [message("relay-recovery-ok")]
        return httpx.Response(200, content=sse(output))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await qualifier.qualify(client, "http://127.0.0.1:4141/codex/v1", "fixture")
    assert len(calls) == 6
    assert report["tool_replay"] == "passed"
    assert report["gui"] == report["compaction"] == "not-tested"
    assert report["cancel_then_request"] == "client-close-then-request"


@pytest.mark.parametrize("body", [
    b"data: [DONE]\n\n", b'data: {"type":"response.failed"}\n\n',
    sse([], "incomplete"), b"data: invalid\n\n",
])
async def test_stream_success_requires_terminal_completion(body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=body))) as client:
        with pytest.raises(ValueError):
            await qualifier.completed(client, "http://127.0.0.1:4141/codex/v1", {"model": "fixture"})


class Stream(httpx.AsyncByteStream):
    def __init__(self, data, *, repeat=False):
        self.data = data
        self.repeat = repeat
        self.closed = False

    async def __aiter__(self):
        while True:
            await anyio.sleep(0.001)
            yield self.data
            if not self.repeat:
                return

    async def aclose(self):
        await anyio.sleep(0.01)
        self.closed = True


@pytest.mark.parametrize("body,encoding", [
    (b"\n" * 1_000_000, "identity"), (b"x" * 2_000_000, "identity"),
    (gzip.compress(b"x" * 4_000_000), "gzip"),
])
async def test_qualification_bounds_all_bytes_before_line_buffering(body, encoding):
    stream = Stream(body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, headers={"content-encoding": encoding}, stream=stream),
    )) as client:
        with pytest.raises(ValueError, match="budget"):
            await qualifier.completed(client, "http://127.0.0.1/codex/v1", {"model": "fixture"})
    assert stream.closed


@pytest.mark.parametrize("body", [b": heartbeat\n\n", b"x"])
async def test_overall_deadline_interrupts_trickling_qualification(body):
    stream = Stream(body, repeat=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=stream))) as client:
        with pytest.raises(TimeoutError):
            await qualifier.qualify(client, "http://127.0.0.1/codex/v1", "fixture", timeout=0.02)
    assert stream.closed


async def test_terminal_completion_does_not_wait_for_eof():
    stream = Stream(sse([message("fixture")]), repeat=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=stream))) as client:
        result = await qualifier.completed(client, "http://127.0.0.1/codex/v1", {"model": "fixture"}, timeout=0.02)
    assert result["status"] == "completed"
    assert stream.closed


@pytest.mark.parametrize("body", [
    b"", b": heartbeat\n\n", sse([message("already done")]),
    b'data: {"type":"response.output_text.delta","delta":"x"}\n\n' + sse([message("done")]),
    b'data: {"type":"response.failed"}\n\n', b"data: [DONE]\n\n",
])
async def test_cancellation_needs_nonterminal_activity(body):
    response = httpx.Response(200, content=body)
    with pytest.raises(ValueError):
        await qualifier.require_active_event(response)


async def test_cancellation_accepts_real_fragmented_activity():
    class Fragmented(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b": heartbeat\n\ndata: "
            yield b'{"type":"response.output_text.delta","delta":"hello"}\n\n'
            pytest.fail("Client should interrupt at active progress.")

    await qualifier.require_active_event(httpx.Response(200, stream=Fragmented()))
