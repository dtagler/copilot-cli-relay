"""Opt-in native checkpoint/resume test with a synthetic transcript and no tools."""
from __future__ import annotations

import argparse
import json
from urllib.parse import urlsplit

import anyio
import httpx

from copilot_cli_relay.proxy_shared import close_response, protect_response, read_complete_bounded

FIXTURE_INPUT = [
    {"role": "user", "content": (
        "Preserve these exact task facts. PROJECT=cedar; WRITE_PERMISSION=denied; "
        "COMPLETED_STEP=read_fixture; NEXT_STEP=report_marker. Never write any files."
    )},
    {"type": "function_call", "name": "read_fixture", "call_id": "call_fixture", "arguments": "{}"},
    {"type": "function_call_output", "call_id": "call_fixture",
     "output": '{"MARKER":"checkpoint-fixture-739","TOTAL":42}'},
    {"role": "assistant", "content": "The read_fixture step is complete. The next step is report_marker."},
]
EXPECTED = "cedar|denied|checkpoint-fixture-739|42|read_fixture|report_marker"
FOLLOWUP = {
    "role": "user",
    "content": (
        "Using only retained context, return these six values in order separated by |, "
        "without spaces or other text: PROJECT, WRITE_PERMISSION, MARKER, TOTAL, COMPLETED_STEP, NEXT_STEP."
    ),
}


def validate_base(value: str) -> str:
    url = urlsplit(value)
    if (url.scheme not in {"http", "https"} or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username or url.password or url.query or url.fragment
            or url.path.rstrip("/") != "/codex/v1"):
        raise ValueError("Use a credential-free loopback /codex/v1 URL.")
    return value.rstrip("/")


async def post_json(client: httpx.AsyncClient, url: str, payload: dict) -> tuple[int, object]:
    request = client.build_request("POST", url, json=payload)
    response = await client.send(request, stream=True)
    protect_response(response)
    try:
        if response.status_code != 200:
            # A status is enough to classify this test. Never echo error bodies.
            return response.status_code, None
        body = await read_complete_bounded(response, 256 * 1024)
        return response.status_code, json.loads(body)
    finally:
        await close_response(response)


async def qualify(client: httpx.AsyncClient, base: str, model: str, *, timeout: float = 120) -> dict:
    with anyio.fail_after(timeout):
        status, compacted = await post_json(client, base + "/responses/compact", {
            "model": model, "input": FIXTURE_INPUT,
            "instructions": "Retain all task facts, constraints, and the fixture tool result for continuation.",
        })
        if status in {404, 501}:
            return {"native_checkpoint": "unavailable", "http_status": status, "continuation": "not-run"}
        if status != 200:
            return {"native_checkpoint": "failed", "http_status": status, "continuation": "not-run"}
        window = compacted.get("output") if isinstance(compacted, dict) else None
        if (not isinstance(compacted, dict) or compacted.get("object") != "response.compaction"
                or not isinstance(window, list) or not window
                or not all(isinstance(item, dict) for item in window)
                or not any(item.get("type") == "compaction" and isinstance(item.get("encrypted_content"), str)
                           and bool(item["encrypted_content"].strip()) for item in window)):
            # An ordinary assistant summary is NOT the native checkpoint contract.
            return {"native_checkpoint": "invalid-checkpoint", "http_status": status, "continuation": "not-run"}
        status, resumed = await post_json(client, base + "/responses", {
            "model": model, "input": [*window, FOLLOWUP], "stream": False, "max_output_tokens": 512,
        })
        if status != 200 or not isinstance(resumed, dict) or resumed.get("status") != "completed":
            return {"native_checkpoint": "received", "http_status": status, "continuation": "failed"}
        output = resumed.get("output", [])
        text = "".join(
            part.get("text", "") for item in output if item.get("type") == "message"
            for part in item.get("content", []) if part.get("type") == "output_text"
        )
        return {"native_checkpoint": "received", "http_status": status,
                "continuation": "passed" if text.strip() == EXPECTED else "facts-not-preserved",
                "gui": "not-tested", "long_session": "not-tested"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:4141/codex/v1")
    parser.add_argument("--run-inference", action="store_true", help="Authorize at most two potentially billable requests.")
    args = parser.parse_args()
    base = validate_base(args.base_url)
    if not args.run_inference:
        print("Plan only: one native compaction probe, then one continuation only if a valid checkpoint is returned.")
        return 0

    async def run():
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=60,
                                    headers={"Authorization": "Bearer dummy"}) as client:
            return await qualify(client, base, args.model)

    try:
        report = anyio.run(run)
        print(json.dumps(report))
        if report.get("continuation") == "passed":
            return 0
        return 2 if report.get("native_checkpoint") == "unavailable" else 1
    except Exception as exc:
        print(json.dumps({"qualification": "failed", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
