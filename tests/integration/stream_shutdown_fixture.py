"""Synthetic loopback upstream/client for network-isolated image drain tests."""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def relay_is_listening() -> bool:
    for path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        if path.exists():
            for row in path.read_text().splitlines()[1:]:
                fields = row.split()
                if fields[1].endswith(":102D") and fields[3] == "0A":
                    return True
    return False


def wait_for_listener_shutdown(listening=relay_is_listening, *, timeout=20, interval=0.05):
    deadline = time.monotonic() + timeout
    while listening():
        if time.monotonic() >= deadline:
            raise TimeoutError("Relay listener did not close for shutdown.")
        time.sleep(interval)


def verify_completion(tail: bytes):
    assert b'"type":"response.completed"' in tail, "Stream did not drain to completion."
    assert b'"fixture_shutdown_observed":true' in tail, "Completion was not synchronized with shutdown."


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if self.path != "/responses" or not 0 < length < 4096:
            self.send_error(400)
            return
        payload = json.loads(self.rfile.read(length))
        if payload.get("model") != "fixture" or payload.get("stream") is not True:
            self.send_error(400)
            return
        if not relay_is_listening():
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"fixture"}\n\n')
        self.wfile.flush()
        # Completion must follow actual listener shutdown, not a five-second
        # timer that could expire before a delayed docker stop invocation.
        wait_for_listener_shutdown()
        self.wfile.write(b'event: response.completed\ndata: {"type":"response.completed","fixture_shutdown_observed":true,"response":{"status":"completed","output":[]}}\n\n')
        self.wfile.flush()
        self.close_connection = True


def probe():
    for url in ("http://127.0.0.1:4141/livez", "http://127.0.0.1:4142/"):
        for _attempt in range(50):
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    assert response.status == 200
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Fixture did not become ready.")


def client():
    request = urllib.request.Request(
        "http://127.0.0.1:4141/codex/v1/responses",
        data=b'{"model":"fixture","stream":true,"input":"synthetic"}',
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        assert response.status == 200
        first = response.readline(4096)
        assert first == b"event: response.output_text.delta\n", "No streamed first event."
        print("stream-started", flush=True)
        tail = response.read(65536)
        verify_completion(tail)
        print("stream-completed", flush=True)


if __name__ == "__main__":
    match sys.argv[1]:
        case "upstream":
            ThreadingHTTPServer(("127.0.0.1", 4142), Upstream).serve_forever()
        case "probe":
            probe()
        case "client":
            client()
        case _:
            raise SystemExit("Unknown fixture mode.")
