# Architecture

`claude-copilot-cli-relay` is a thin reverse proxy that lets [Claude Code](https://docs.anthropic.com/en/docs/claude-code) talk to **GitHub Copilot Enterprise** instead of the Anthropic API. Because Copilot exposes a *native* Anthropic Messages endpoint at `https://api.githubcopilot.com/v1/messages`, the proxy is mostly a header-and-auth swap, not a protocol translator.

This file is the contract for anyone modifying the proxy: what each piece does, where the non-obvious behavior lives, and what *must not* break.

---

## High-level data flow

```
┌──────────────┐  Anthropic JSON   ┌─────────────────────────┐  Anthropic JSON   ┌──────────────────────┐
│ Claude Code  │ ────────────────▶ │ claude-copilot-cli-relay│ ────────────────▶ │ api.githubcopilot.com│
│   (CLI)      │ ◀─── SSE/JSON ─── │   127.0.0.1:4141        │ ◀─── SSE/JSON ─── │ /v1/messages, /models│
└──────────────┘                   │   uvicorn + Starlette   │                   └──────────────────────┘
                                   │   + httpx (HTTP/2)      │
                                   └─────────────┬───────────┘
                                                 │
                                                 ├── reads OAuth token from .env (COPILOT_GITHUB_TOKEN)
                                                 ├── strips client auth, sets its own Bearer + Copilot-Integration-Id
                                                 └── rewrites reasoning-effort fields per model
```

Three things make this work cleanly:

1. The `copilot` CLI's OAuth token (Windows Credential Manager) authenticates **directly** against `api.githubcopilot.com`. No token-exchange step.
2. Copilot's `/v1/messages` speaks the Anthropic protocol natively for Claude models — proper SSE event names, no transcoding.
3. `Copilot-Integration-Id: copilot-developer-cli` is what unlocks Claude models on Copilot Enterprise. Other integration ids (`vscode-chat`, etc.) only expose GPT models for many tenants.

---

## Three routes

| Method & path | Handler | Purpose |
|---|---|---|
| `POST /v1/messages` | `proxy_messages` | Forward chat completion request; supports streaming and non-streaming |
| `GET  /v1/models` | `proxy_models` | Fetch upstream models, filter to Anthropic chat-eligible, canonicalize ids to dash form |
| `GET  /healthz` | `healthz` | Liveness + upstream reachability + Anthropic model count |

All three are registered in [`server.py`](src/claude_copilot_cli_relay/server.py) and bound to a shared `httpx.AsyncClient(http2=True)` created at app startup.

---

## File map

```
src/claude_copilot_cli_relay/
├── __init__.py          # version constant
├── __main__.py          # `python -m claude_copilot_cli_relay` entrypoint (defaults 127.0.0.1; rarely used outside the container)
├── server.py            # Starlette app + lifespan-managed httpx.AsyncClient
├── config.py            # Settings dataclass; env parsing; gho_/ghu_ prefix check
├── headers.py           # build_outbound_headers() — strip + rebuild for upstream
├── proxy.py             # the three handlers + streaming/keepalive plumbing
├── errors.py            # Anthropic-shaped JSON + SSE error envelopes
└── logging_setup.py     # configure_logging() + redact_text/redact_bytes
```

Tests mirror that under `tests/unit/`.

---

## Request lifecycle: `POST /v1/messages`

1. **Read body once.** `_parse_request(raw_body)` performs a single `json.loads` and returns `(rewritten_body, model, streaming)`. Malformed bodies pass through unchanged so the client sees Copilot's actual error message.
2. **Rewrite the body for Copilot.** `_apply_effort_rewrite` clamps `reasoning_effort` and `output_config.effort` to per-model allow-lists in `_EFFORT_OVERRIDES`. Examples:
   - Opus 4.7 → only `medium` is accepted; `high`/`low` are silently downgraded.
   - Haiku 4.5 → reasoning effort isn't supported at all; the field is *removed* (not clamped).
   - Empty `output_config` after stripping is dropped entirely.
   Without this, Claude Code's `xhigh`/`extreme`/`minimal` aliases would 400 upstream.
3. **Build outbound headers.** `build_outbound_headers` (in `headers.py`):
   - Strips RFC 7230 §6.1 hop-by-hop headers (`Connection`, `Keep-Alive`, `Proxy-Authenticate`, `Proxy-Authorization`, `TE`, `Trailer`, `Trailers`, `Transfer-Encoding`, `Upgrade`) plus `Host`, `Content-Length`.
   - **Strips inbound `Authorization`, `x-api-key`, `User-Agent`, `Cookie`, `proxy-authorization`** (any client-supplied auth or session state must NEVER reach upstream — this is a security invariant with regression tests). Upstream `Set-Cookie` is dropped on the way back.
   - Filters `anthropic-beta` for `UNSUPPORTED_BETA_TOKENS` (currently `context-1m-2025-08-07`, which Copilot rejects).
   - Sets `Authorization: Bearer <COPILOT_GITHUB_TOKEN>`, `Copilot-Integration-Id`, `Editor-Version`, `User-Agent`, `X-Request-Id`, and (case-insensitive defaulted) `anthropic-version`.
4. **Dispatch by streaming flag.**
   - **Non-streaming (`stream: false`):** `await client.post(...)`, then `_passthrough_response(resp)` strips response-side hop-by-hop headers and wraps in a Starlette `Response`.
   - **Streaming (`stream: true`):** `client.stream(...)` → `_stream_response()` → SSE chunks yielded through `_chunks_with_keepalive()`. See next section.
5. **Errors.** `httpx.TimeoutException` and `httpx.HTTPError` are caught and converted to Anthropic-shaped envelopes (`json_error` for non-stream, `sse_error_event` mid-stream).

---

## Streaming: the keepalive design

The single most subtle piece of the codebase. The naïve implementation — wrapping `await aiter_raw().__anext__()` in `anyio.move_on_after(15)` to send pings — **silently truncates streams**, because cancelling the awaited `__anext__()` finalizes the underlying async generator (PEP 525). After the first 15s quiet interval, subsequent reads return `StopAsyncIteration` and the SSE stream ends without an error frame.

`_chunks_with_keepalive` solves this with a producer/consumer pattern:

```
                 ┌──────────────────────┐
upstream.aiter_bytes() ─▶│ producer task    │──▶ memory_object_stream(buffer=8) ──▶┐
                 └──────────────────────┘                                         │
                                                                                  ▼
                                                ┌─────────────────────────────────────┐
                                                │ consumer loop                        │
                                                │  while True:                         │
                                                │    if request.is_disconnected():     │
                                                │       yield disconnect, return       │
                                                │    with move_on_after(15):           │
                                                │       chunk = await recv.receive()   │
                                                │    if scope.cancel_called:           │
                                                │       yield ping; continue           │
                                                │    yield chunk                       │
                                                └─────────────────────────────────────┘
```

Key invariants:

- **Ping timeout cancels `recv.receive()`, NOT the upstream HTTP read.** The upstream read keeps running in the background producer; cancelling a memory-channel read is harmless.
- **Producer exceptions are captured, not swallowed.** They're stashed in a `producer_error` list and re-raised by the consumer *outside* the `anyio.create_task_group()` block (so they don't get wrapped in an `ExceptionGroup`). The outer `except Exception` in `body_iter` then yields an `sse_error_event` so the client sees a terminal `event: error` instead of a silently-truncated stream.
- **Client disconnect short-circuits cleanly.** Yields a `("", "disconnect")` sentinel, cancels the task group, and `aclose`'s the upstream response in a `finally`.
- **Channel buffer = 8** keeps backpressure modest: a slow client can stall the producer after 8 buffered chunks, which is desired for memory safety.
- **Decoding:** the producer uses `aiter_bytes()` (not `aiter_raw()`), so any upstream `Content-Encoding` (gzip/br/deflate) is transparently decoded by httpx. Forwarding still-compressed bytes with the `text/event-stream` content-type would silently corrupt the stream.
- **Pre-stream errors return real HTTP status.** `_stream_response` opens the upstream with `client.send(req, stream=True)` and inspects `upstream.status_code` *before* returning a `StreamingResponse`. If the upstream answered ≥400 before any body byte was forwarded, the proxy returns a `JSONResponse` with the upstream's HTTP status (`401`, `429`, …) and an Anthropic error envelope. Only failures observed *after* the stream has started surface as a terminal `event: error` SSE frame. This lets SDK retry logic that keys on HTTP status (`401` → re-auth, `429` → backoff) fire correctly. The bounded-read helper (`_read_bounded`, 32 KiB cap) prevents a hostile or runaway upstream error body from being read into memory unbounded.

---

## Request lifecycle: `GET /v1/models`

`proxy_models` calls `GET {api_base}/models`, then filters and reshapes the result:

- **Vendor filter:** keeps only `vendor == "Anthropic"` (case-insensitive).
- **Capability filter:** keeps only `capabilities.type == "chat"`.
- **Picker filter:** drops models with `model_picker_enabled: false`.
- **Hide internal-only experiments without 1M context:** drops models whose name contains "internal only" *unless* the id ends in `-1m` / `-1m-internal`. The 1M-context variants happen to also be flagged "(Internal only)" by upstream — keeping them is the only way to actually get 1M context on this Copilot tenant (verified via `capabilities.limits.max_context_window_tokens=1_000_000`).
- **Canonicalize ids:** dot form → dash form (`claude-opus-4.7` → `claude-opus-4-7`) — *every* id, including `-1m`. Claude Code's `/model` slash command and built-in name recognition both validate against the canonical dash form, so anything we expose has to be in that shape. The dash→dot conversion that Copilot's `/v1/messages` requires for `-1m` ids ("model_not_supported" otherwise) is applied as a last hop in `proxy_messages._normalize_model_for_upstream`, so the user-facing id stays dash everywhere Claude Code touches it.
- **Defensive parsing:** non-JSON `200` bodies (captive portals, stray HTML) are caught and returned as a `502 api_error` envelope rather than 500-ing. Each model item is also `isinstance`-checked field-by-field so a malformed-but-valid-JSON shape from upstream can't `AttributeError` past the error envelope.
- **1M context routing:** see `_remap_to_1m` in `proxy_messages` — when the inbound request carries `anthropic-beta: context-1m-2025-08-07` (Claude Code's hardcoded "Opus 4.7 (1M context)" picker tier) and the model is one of `claude-opus-4-7` / `claude-opus-4-6` (or their dot forms), the body's `model` is rewritten to upstream's `claude-opus-4-7-1m-internal` / `claude-opus-4-6-1m` variant (still dash; the dash→dot wire conversion happens in `_normalize_model_for_upstream`). The beta header itself is still stripped in `headers.py` because Copilot rejects it on every model id; the `-1m` model id is the real 1M switch. Without this remap, those picker tiers would silently downgrade to 200K context.

The response uses Anthropic's standard list envelope (`data`, `has_more`, `first_id`, `last_id`).

---

## Configuration

All runtime config comes from environment variables, parsed in `config.py:Settings.from_env()`. The dataclass is frozen; tests inject via `reset_settings_for_tests()` rather than mutating env.

| Var | Default | Purpose |
|---|---|---|
| `COPILOT_GITHUB_TOKEN` | — | **Required.** Must start `gho_`/`ghu_`; rejects PATs (`ghp_`) early with a clear message |
| `COPILOT_PROXY_PORT` | `4141` | uvicorn listen port. Read only by the `python -m claude_copilot_cli_relay` entrypoint. The Docker `CMD` hardcodes `--port 4141`, so this env var has no effect when running via `docker compose` — change the published port in `docker-compose.yml` instead |
| `COPILOT_PROXY_HOST` | `127.0.0.1` | Bind interface used by the `python -m claude_copilot_cli_relay` entrypoint. The Docker entrypoint (`uvicorn ... --host 0.0.0.0`) ignores it and binds `0.0.0.0` inside the container; the host-side loopback bind is enforced by `docker-compose.yml`'s `127.0.0.1:4141:4141` publish. The supported way to run the proxy is `docker compose up`; `python -m claude_copilot_cli_relay` exists as an in-container debugging entrypoint and is rarely needed |
| `COPILOT_API_BASE` | `https://api.githubcopilot.com` | Override upstream (e.g. mocks). **Must use `https://`** unless `COPILOT_ALLOW_INSECURE_API_BASE=1`; the proxy injects the OAuth bearer into every outbound request, so a plaintext or unintended host would leak the credential |
| `COPILOT_ALLOW_INSECURE_API_BASE` | `0` | Set to `1` to allow `http://` `COPILOT_API_BASE` (test mocks, never production) |
| `COPILOT_INTEGRATION_ID` | `copilot-developer-cli` | **Do not change** unless you know your tenant requires a different value |
| `COPILOT_EDITOR_VERSION` | `claude-copilot-cli-relay/0.1.0` | Sent as `Editor-Version` and `User-Agent` |
| `LOG_LEVEL` | `info` | `debug` enables header-level debug logging |
| `LOG_BODIES` | `0` | `1` enables redacted body logging. Bodies contain the user's source code |

---

## Logging and redaction

`logging_setup.redact_text` is applied to any logged body. It covers:

- GitHub tokens: classic `gh[ousrp]_…` and fine-grained `github_pat_…`
- JWTs: `eyJ…\.…\.…`
- OpenAI / Anthropic style: `sk-…`, `sk-ant-…`, `sk-proj-…`
- AWS access key ids (`AKIA[0-9A-Z]{16}`) and AWS secret access keys when introduced by an `aws_secret_access_key=` style key name
- Slack tokens (`xox[baprs]-…`), Stripe live/restricted keys (`sk_live_…`, `rk_live_…`), Google API keys (`AIza…`)
- Header lines: `Authorization`, `x-api-key`, `api-key`, `proxy-authorization` (both raw `Header: value` form and quoted dict-/JSON-repr form e.g. `"Authorization": "Basic …"`)
- Generic `Bearer <token>` not on a header line
- JSON keys: `api_key`, `access_token`, `auth_token`, `secret`, `password`, `client_secret`, `refresh_token`, `private_key`
- Exception messages on every error path also pass through `redact_text` before being logged or echoed to the client.

`redact_bytes` additionally:
- Truncates inline `data:…;base64,…` URIs (avoid logging entire screenshots).
- Truncates the whole body to 16 KiB.

The token prefix (e.g. `gho_`) is preserved in redaction output to aid debugging without revealing the secret.

---

## Security model

- **Bind:** `127.0.0.1:4141` only. The Docker port-publish enforces this; the `__main__` entrypoint also defaults to it.
- **No client-supplied auth ever reaches upstream.** `Authorization`, `x-api-key`, `proxy-authorization`, `Cookie`, and `User-Agent` are stripped in `headers.build_outbound_headers`; regression-tested. Upstream `Set-Cookie` is similarly dropped from responses returned to the client.
- **Token storage:** read fresh from `.env` at container start; never persisted by the proxy. The `extract-token.ps1` helper writes `.env` with a hardened ACL — inheritance disabled and dropped, then explicit ACEs for the current user and `NT AUTHORITY\SYSTEM`. In practice, the resulting on-disk DACL also includes `BUILTIN\Administrators` with FullControl when the script is run by an account in that group; the script does not strip this. The script *fails closed* if the ACL operation can't be applied — it refuses to leave a token on disk under default permissions.
- **Bodies off by default.** `LOG_BODIES=0`. When enabled, redaction runs on every body.
- **Container runs non-root** (UID 1000).
- **`.gitignore` and `.dockerignore`** both block `.env` and `.env.*` while allowing `.env.example`. Verified with `git check-ignore`.
- **Upstream errors echo at most 300 chars** of the upstream body in the error envelope. The hop-by-hop strip prevents `Authorization` from being reflected back if upstream ever did so.

What this *isn't* hardened against: a malicious local web page making cross-origin POSTs to `localhost:4141`. There's no Origin check; if you're worried about that, run a separate browser profile or lock down host firewall.

---

## Testing

Tests live in `tests/unit/` and run inside the same Docker image as production. **There is no host-side Python or `venv` involved** — the README's "Development" section explains the full Docker-only workflow, including how to add or upgrade dependencies via throwaway containers.

```powershell
docker compose run --rm proxy uv run pytest tests/unit -q
```

For coverage:

```powershell
docker compose run --rm proxy bash -c "uv run --with coverage coverage run --source=src/claude_copilot_cli_relay -m pytest tests/unit -q && uv run --with coverage coverage report"
```

Current state: 173 tests, 99% coverage. The 3 unreached lines are the `__main__` script-guard (which pytest never invokes) and a defensive bare-except in `redact_bytes` that can't trigger after the preceding `decode(errors="replace")` call.

Coverage hot-spots that matter:
- `_chunks_with_keepalive` — both happy-path (chunks-after-pings regression) and producer-exception propagation are covered.
- Inbound auth-leak — explicit test asserts `Authorization`, `x-api-key`, `proxy-authorization`, `Cookie` never reach upstream.
- Body redaction — every pattern has a positive test.
- Hop-by-hop headers — both `Trailer` (singular) and `Trailers` are covered on inbound and outbound.

---

## Don't touch

- **`Copilot-Integration-Id` default.** It's specifically the value that unlocks Claude models on this tenant.
- **Streaming/keepalive structure.** It looks over-engineered; it isn't. See the keepalive section above.
- **`/v1/models` filtering rules.** Each rule is there because Claude Code misbehaves without it.
- **`gho_`/`ghu_` prefix check in `config.py`.** It catches the common mistake of pasting a PAT (`ghp_`).
- **Strict `--frozen` in the Dockerfile.** It exists to fail-fast on lockfile drift; do not add a `|| uv lock` fallback.
