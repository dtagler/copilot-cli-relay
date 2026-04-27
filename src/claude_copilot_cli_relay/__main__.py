"""Run with `python -m claude_copilot_cli_relay` for local (host) debugging.

Defaults to binding 127.0.0.1 — the same loopback-only stance the docker-compose
service enforces. To listen on a different interface, set `COPILOT_PROXY_HOST`
(e.g. `0.0.0.0`) explicitly. The Docker entrypoint in the Dockerfile passes
`--host 0.0.0.0` directly to uvicorn because Docker's port-publish layer is
what enforces the loopback bind there.
"""
from __future__ import annotations

import os

import uvicorn

from .config import get_settings


def main() -> None:
    s = get_settings()
    host = os.environ.get("COPILOT_PROXY_HOST", "127.0.0.1")
    uvicorn.run("claude_copilot_cli_relay.server:app", host=host, port=s.proxy_port, log_level=s.log_level)


if __name__ == "__main__":
    main()
