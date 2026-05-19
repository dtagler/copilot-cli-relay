"""Outbound header construction for Claude and Codex upstream Copilot requests."""
from __future__ import annotations

import uuid
from collections.abc import Mapping

_CLAUDE_STRIP_HEADERS = frozenset(
    h.lower()
    for h in (
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "authorization",
        "x-api-key",
        "user-agent",
        # Defensive: never forward client-supplied cookies upstream. Claude Code
        # doesn't send them, but anything else hitting the loopback port might.
        "cookie",
    )
)
# Beta header tokens Copilot's /v1/messages does NOT accept.
# Claude Code may send these unconditionally; strip them so requests don't 400.
# Stored lowercased; comparisons must lowercase the inbound token too —
# otherwise a mixed-case `Context-1M-2025-08-07` would slip past the strip
# and reach upstream (which rejects it on every model id).
UNSUPPORTED_BETA_TOKENS = frozenset({
    "context-1m-2025-08-07",
    # Claude Code sends this on every request; Copilot's /v1/messages rejects it
    # ("unsupported beta header(s): advisor-tool-2026-03-01"), 400-ing the call.
    "advisor-tool-2026-03-01",
})


def _filter_anthropic_beta(value: str) -> str | None:
    tokens = [t.strip() for t in value.split(",") if t.strip()]
    kept = [t for t in tokens if t.lower() not in UNSUPPORTED_BETA_TOKENS]
    return ", ".join(kept) if kept else None


def _strip_claude_inbound_headers(inbound: Mapping[str, str]) -> dict[str, str]:
    dynamic_strip: set[str] = set()
    for name, value in inbound.items():
        if name.lower() == "connection":
            dynamic_strip.update(t.strip().lower() for t in value.split(",") if t.strip())

    out: dict[str, str] = {}
    for name, value in inbound.items():
        lname = name.lower()
        if lname in _CLAUDE_STRIP_HEADERS or lname in dynamic_strip:
            continue
        out[name] = value
    return out


def build_claude_outbound_headers(
    inbound: Mapping[str, str],
    *,
    bearer_token: str,
    integration_id: str,
    editor_version: str,
    request_id: str | None = None,
) -> dict[str, str]:
    out = {}
    for name, value in _strip_claude_inbound_headers(inbound).items():
        lname = name.lower()
        if lname == "anthropic-beta":
            filtered = _filter_anthropic_beta(value)
            if filtered:
                out[name] = filtered
            continue
        out[name] = value

    out["Authorization"] = f"Bearer {bearer_token}"
    out["Copilot-Integration-Id"] = integration_id
    out["Editor-Version"] = editor_version
    out["User-Agent"] = editor_version
    out["X-Request-Id"] = request_id or str(uuid.uuid4())

    # Use case-insensitive presence check — inbound dicts may carry mixed case.
    if not any(k.lower() == "anthropic-version" for k in out):
        out["anthropic-version"] = "2023-06-01"
    return out


def build_codex_outbound_headers(
    inbound: Mapping[str, str],
    *,
    bearer_token: str,
    integration_id: str,
    editor_version: str,
    plugin_version: str,
    user_agent: str,
    github_api_version: str,
    session_id: str,
    machine_id: str,
    request_id: str | None = None,
    initiator: str = "agent",
    accept: str = "application/json",
) -> dict[str, str]:
    # Rebuild from scratch; no client auth/session state should ride to Copilot.
    _ = inbound
    return {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "Accept": accept,
        "Copilot-Integration-Id": integration_id,
        "Editor-Version": editor_version,
        "Editor-Plugin-Version": plugin_version,
        "User-Agent": user_agent,
        "OpenAI-Intent": "conversation-panel",
        "X-Interaction-Type": "conversation-panel",
        "X-GitHub-Api-Version": github_api_version,
        "X-VSCode-User-Agent-Library-Version": "electron-fetch",
        "VScode-SessionId": session_id,
        "VScode-MachineId": machine_id,
        "X-Initiator": initiator,
        "X-Request-Id": request_id or str(uuid.uuid4()),
    }
