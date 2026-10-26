"""Opt-in synthetic Responses qualification. Never executes model-generated tools."""
from __future__ import annotations

import argparse
import json
from urllib.parse import urlsplit

import anyio
import httpx

from copilot_cli_relay.proxy_shared import close_response, decoded_chunks, protect_response


def validate_base(value: str) -> str:
    url = urlsplit(value)
    if (url.scheme not in {"http", "https"} or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username or url.password or url.query or url.fragment
            or url.path.rstrip("/") != "/codex/v1"):
        raise ValueError("Use a credential-free loopback /codex/v1 URL.")
    return value.rstrip("/")


async def completed(client: httpx.AsyncClient, base: str, payload: dict, *, timeout: float = 60) -> dict:
    total = 0
    data: list[str] = []
    pending = b""
    response = None
    try:
        with anyio.fail_after(timeout):
            request = client.build_request("POST", base + "/responses",
                                           json={**payload, "stream": True, "max_output_tokens": 512})
            response = await client.send(request, stream=True)
            protect_response(response)
            response.raise_for_status()
            async for chunk in decoded_chunks(response):
                # Count every decoded byte before line/event buffering, including
                # comments and blank lines. Decoder allocations are also bounded.
                total += len(chunk)
                if total > 256 * 1024:
                    raise ValueError("Qualification stream exceeded its budget.")
                lines = (pending + chunk).split(b"\n")
                pending = lines.pop()
                for raw in lines:
                    line = raw.rstrip(b"\r").decode("utf-8")
                    if line.startswith("data:"):
                        data.append(line[5:].strip())
                    elif not line and data:
                        payload_text = "\n".join(data)
                        data.clear()
                        if payload_text == "[DONE]":
                            continue
                        event = json.loads(payload_text)
                        if event.get("type") in {"error", "response.failed", "response.incomplete"}:
                            raise ValueError("Qualification response failed.")
                        if event.get("type") == "response.completed":
                            terminal = event.get("response")
                            if not isinstance(terminal, dict) or terminal.get("status") != "completed":
                                raise ValueError("No successful terminal completion.")
                            return terminal  # Do not wait for EOF after completion.
        raise ValueError("No successful terminal completion.")
    finally:
        if response is not None:
            await close_response(response)


def text_of(response: dict) -> str:
    return "".join(
        part.get("text", "") for item in response.get("output", []) if item.get("type") == "message"
        for part in item.get("content", []) if part.get("type") == "output_text"
    )


async def qualify(client: httpx.AsyncClient, base: str, model: str, *, timeout: float = 180) -> dict:
    with anyio.fail_after(timeout):
        return await _qualify(client, base, model)

async def require_active_event(response: httpx.Response) -> None:
    """Observe real response progress before intentionally interrupting the stream."""
    total = 0
    pending = b""
    data: list[str] = []
    async for chunk in decoded_chunks(response):
        total += len(chunk)
        if total > 256 * 1024:
            raise ValueError("Cancellation stream exceeded its budget.")
        lines = (pending + chunk).split(b"\n")
        pending = lines.pop()
        active = False
        for raw in lines:
            line = raw.rstrip(b"\r").decode("utf-8")
            if line.startswith("data:"):
                data.append(line[5:].strip())
            elif not line and data:
                value = "\n".join(data)
                data.clear()
                if value == "[DONE]":
                    raise ValueError("Cancellation response already ended.")
                event = json.loads(value)
                event_type = event.get("type")
                if event_type in {"error", "response.failed", "response.incomplete", "response.completed"}:
                    raise ValueError("Cancellation response ended before interruption.")
                if event_type in {"response.in_progress", "response.output_text.delta",
                                  "response.function_call_arguments.delta"}:
                    active = True
        # Inspect the whole received chunk, so a terminal event alongside a delta
        # does not become a false positive. A partial next event is fine to stop.
        if active:
            return
    raise ValueError("No active response event before cancellation.")


async def _qualify(client: httpx.AsyncClient, base: str, model: str) -> dict:
    first = await completed(client, base, {"model": model, "input": "Reply with exactly relay-qualification-ok."})
    if "relay-qualification-ok" not in text_of(first):
        raise ValueError("Text smoke did not return its marker.")
    tool = {"type": "function", "name": "read_fixture", "description": "Read the synthetic fixture.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False}, "strict": True}
    history = [{"role": "user", "content": "Read the fixture using read_fixture, then report its marker."}]
    call_response = await completed(client, base, {
        "model": model, "input": history, "tools": [tool],
        "tool_choice": {"type": "function", "name": "read_fixture"},
    })
    calls = [item for item in call_response.get("output", []) if item.get("type") == "function_call"]
    if len(calls) != 1 or calls[0].get("name") != "read_fixture" or not calls[0].get("call_id"):
        raise ValueError("Expected one allowlisted fixture call.")
    # No command execution or file access. Only a fixed synthetic tool result.
    history += call_response["output"]
    history.append({"type": "function_call_output", "call_id": calls[0]["call_id"], "output": "fixture-marker-739"})
    answer = await completed(client, base, {"model": model, "input": history, "tools": [tool], "tool_choice": "none"})
    if "fixture-marker-739" not in text_of(answer):
        raise ValueError("Tool-result replay failed.")
    history += answer["output"]
    history.append({"role": "user", "content": "Repeat the fixture marker from our conversation."})
    followup = await completed(client, base, {"model": model, "input": history})
    if "fixture-marker-739" not in text_of(followup):
        raise ValueError("Follow-up replay failed.")
    request = client.build_request("POST", base + "/responses", json={
        "model": model, "input": "Count slowly from 1 to 100.", "stream": True, "max_output_tokens": 256,
    })
    response = await client.send(request, stream=True)
    protect_response(response)
    try:
        response.raise_for_status()
        await require_active_event(response)
    finally:
        await close_response(response)
    recovered = await completed(client, base, {"model": model, "input": "Reply with exactly relay-recovery-ok."})
    if "relay-recovery-ok" not in text_of(recovered):
        raise ValueError("Request after cancellation failed.")
    return {"streaming": "passed", "tool_replay": "passed", "followup": "passed",
            "cancel_then_request": "client-close-then-request", "gui": "not-tested", "compaction": "not-tested"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:4141/codex/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--run-inference", action="store_true", help="Authorize six synthetic, potentially billable requests.")
    args = parser.parse_args()
    base = validate_base(args.base_url)
    if not args.run_inference:
        print("Plan only: six synthetic inference requests; no tools executed. Add --run-inference only after approval.")
        return 0
    try:
        async def run():
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=60,
                                        headers={"Authorization": "Bearer dummy"}) as client:
                return await qualify(client, base, args.model)

        print(json.dumps(anyio.run(run)))
        return 0
    except Exception as exc:
        # Do not print model output, prompts, raw HTTP errors, or credentials.
        print(json.dumps({"qualification": "failed", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
