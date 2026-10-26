"""Native checkpoint qualification contract and privacy controls, without inference."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import anyio
import httpx
import pytest

_path = Path(__file__).resolve().parents[2] / "scripts/qualify-checkpoint.py"
_spec = importlib.util.spec_from_file_location("qualify_checkpoint", _path)
checkpoint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checkpoint)


def test_plan_does_not_construct_client(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["qualify-checkpoint.py", "--model", "fixture"])
    monkeypatch.setattr(checkpoint.httpx, "AsyncClient", lambda **_kw: pytest.fail("Plan attempted network."))
    assert checkpoint.main() == 0
    assert "Plan only" in capsys.readouterr().out


@pytest.mark.parametrize("base", ["https://external.example/codex/v1", "http://user:password@127.0.0.1/codex/v1",
                                "http://127.0.0.1/codex/v1?token=x", "http://127.0.0.1/wrong"])
def test_external_or_credential_urls_rejected(base):
    with pytest.raises(ValueError):
        checkpoint.validate_base(base)


@pytest.mark.parametrize("status,result", [(404, "unavailable"), (501, "unavailable"), (401, "failed"), (429, "failed")])
async def test_unavailable_or_failed_checkpoint_never_attempts_resume(status, result):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text="sensitive upstream error")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await checkpoint.qualify(client, "http://127.0.0.1/codex/v1", "fixture")
    assert len(requests) == 1
    assert report == {"native_checkpoint": result, "http_status": status, "continuation": "not-run"}
    assert "sensitive" not in json.dumps(report)


@pytest.mark.parametrize("payload", [
    None, {"object": "response.compaction", "output": []},
    {"object": "response.compaction", "output": [{"type": "message", "content": [{"type": "output_text", "text": "summary"}]}]},
    {"object": "response.compaction", "output": [{"type": "compaction", "encrypted_content": ""}]},
])
async def test_summary_or_malformed_output_does_not_count_as_checkpoint(payload):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, content=json.dumps(payload).encode()),
    )) as client:
        report = await checkpoint.qualify(client, "http://127.0.0.1/codex/v1", "fixture")
    assert report["native_checkpoint"] == "invalid-checkpoint"
    assert report["continuation"] == "not-run"


@pytest.mark.parametrize("text,expected", [(checkpoint.EXPECTED, "passed"), ("cedar|allowed", "facts-not-preserved")])
async def test_resume_uses_complete_returned_window_and_checks_all_facts(text, expected):
    requests = []
    window = [{"role": "user", "content": "retained"}, {"type": "compaction", "encrypted_content": "opaque-fixture"}]

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["model"] == "fixture"
        if len(requests) == 1:
            assert payload["input"] == checkpoint.FIXTURE_INPUT
            return httpx.Response(200, json={"object": "response.compaction", "output": window})
        assert payload["input"] == [*window, checkpoint.FOLLOWUP]
        assert payload["max_output_tokens"] == 512
        assert "tools" not in payload
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await checkpoint.qualify(client, "http://127.0.0.1/codex/v1", "fixture")
    assert len(requests) == 2
    assert report["continuation"] == expected
    assert report["gui"] == report["long_session"] == "not-tested"


async def test_checkpoint_deadline_closes_slow_stream():
    class Slow(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            await anyio.sleep(1)
            yield b"{}"

        async def aclose(self):
            await anyio.sleep(0.01)
            self.closed = True

    stream = Slow()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=stream))) as client:
        with pytest.raises(TimeoutError):
            await checkpoint.qualify(client, "http://127.0.0.1/codex/v1", "fixture", timeout=0.01)
    assert stream.closed
