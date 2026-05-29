# Architecture

`copilot-cli-relay` is a thin reverse proxy that lets [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and OpenAI Codex CLI talk to **GitHub Copilot Enterprise** instead of the Anthropic/OpenAI APIs. Because Copilot exposes native protocol endpoints at `https://api.githubcopilot.com/v1/messages` for Claude models and `https://api.githubcopilot.com/responses` for GPT/Codex models, the proxy is mostly a header-and-auth swap, not a protocol translator.

This file is the contract for anyone modifying the proxy: what each piece does, where the non-obvious behavior lives, and what *must not* break.

---

## High-level data flow

```text
+==============================+       +========================================+       +================================+
| Claude Code                  |       | copilot-cli-relay                      |       | GitHub Copilot Enterprise      |
| Anthropic Messages JSON/SSE  |       | 127.0.0.1:4141                         |       | Anthropic native endpoints     |
| POST /claude/v1/messages     | ====> | Claude route handlers                  | ====> | POST /v1/messages              |
| GET  /claude/v1/models       | <==== | build_claude_outbound_headers          | <==== | GET  /models                   |
+==============================+       |                                        |       +================================+
                                       | uvicorn + Starlette                    |
                                       | shared httpx AsyncClient, HTTP/2       |
                                       | proxy_shared streaming + passthrough   |
+==============================+       |                                        |       +================================+
| Codex CLI                    |       | Codex route handlers                   |       | GitHub Copilot Enterprise      |
| OpenAI Responses JSON/SSE    |       | build_codex_outbound_headers           |       | Responses native endpoints     |
| POST /codex/v1/responses     | ====> | compact fallback, body cleanup         | ====> | POST /responses                |
| POST /codex/v1/responses/    | ====> |                                        | ====> | POST /responses/compact        |
| compact                      |       |                                        |       | or fallback to /responses      |
| GET  /codex/v1/models        | <==== |                                        | <==== | GET  /models                   |
+==============================+       +====================^===================+       +================================+
                                                            |
                                                            |
                                      .env: COPILOT_GITHUB_TOKEN
                                      extracted from Windows Credential Manager
```

Four things make this work cleanly:

1. The `copilot` CLI's OAuth token (Windows Credential Manager) authenticates **directly** against `api.githubcopilot.com`. No token-exchange step.
2. Copilot's `/v1/messages` speaks the Anthropic protocol natively for Claude models — proper SSE event names, no transcoding.
3. Copilot's `/responses` speaks the OpenAI Responses protocol natively for GPT/Codex models.
4. `Copilot-Integration-Id: copilot-developer-cli` is what unlocks Claude and GPT-5 Responses models on this Copilot Enterprise tenant. Other integration ids (`vscode-chat`, etc.) may expose different catalogs and rejected `gpt-5.5` during live testing.

---

## Routes

| Method & path | Handler | Purpose |
|---|---|---|
| `POST /claude/v1/messages` | `proxy_claude_messages` | Forward Claude/Anthropic request; supports streaming and non-streaming |
| `GET  /claude/v1/models` | `proxy_claude_models` | Fetch upstream models, filter to Anthropic chat-eligible, canonicalize ids to dash form |
| `POST /codex/v1/responses` | `proxy_codex_responses` | Forward Codex/OpenAI Responses requests to Copilot `/responses` |
| `POST /codex/v1/responses/compact` | `proxy_codex_responses_compact` | Try native Responses compaction, then synthesize compaction if upstream 404s |
| `GET  /codex/v1/models` | `proxy_codex_models` | Fetch upstream models, filter to `/responses`-capable models, return both OpenAI list and Codex catalog shapes |
| `GET  /claude/healthz` | `claude_healthz` | Liveness + upstream reachability + Anthropic model count |
| `GET  /codex/healthz` | `codex_healthz` | Liveness + upstream reachability + Responses model count |
| `GET  /healthz` | `claude_healthz` | Backward-compatible alias for `/claude/healthz` |

All routes are registered in [`server.py`](src/copilot_cli_relay/server.py) and bound to a shared `httpx.AsyncClient(http2=True)` created at app startup.

---

## File map

```
src/copilot_cli_relay/
├── __init__.py          # version constant
├── __main__.py          # `python -m copilot_cli_relay` entrypoint (defaults 127.0.0.1; rarely used outside the container)
├── server.py            # Starlette app + lifespan-managed httpx.AsyncClient
├── config.py            # Settings dataclass; env parsing; gho_/ghu_ prefix check
├── headers.py           # Anthropic and Codex header builders — strip + rebuild for upstream
├── claude_proxy.py      # Claude/Anthropic handlers
├── codex_proxy.py       # Codex/OpenAI Responses handlers
├── model_capabilities.py # ModelCapabilityCache: live /models reasoning-effort caps (stale-while-revalidate)
├── security.py          # Loopback-host + local-browser guard middleware
├── proxy_shared.py      # Shared response filtering, bounded reads, and keepalive streaming
├── errors.py            # Anthropic-shaped and OpenAI-shaped JSON + SSE error envelopes
└── logging_setup.py     # configure_logging() + redact_text/redact_bytes
```

Tests mirror that under `tests/unit/`.

---

## Request lifecycle: `POST /claude/v1/messages`

1. **Read body once.** `_parse_claude_request(raw_body, caps)` performs a single `json.loads` and returns `(rewritten_body, model, streaming)`. Malformed bodies pass through unchanged so the client sees Copilot's actual error message.
2. **Rewrite the body for Copilot.** Two massaging steps run before forwarding:
   - `_apply_thinking_rewrite` converts `thinking: {type: "enabled", budget_tokens: N}` (the legacy extended-thinking shape Claude Code sends) into `thinking: {type: "adaptive"}` and drops `budget_tokens`. Copilot's Anthropic endpoint now rejects `thinking.type.enabled` outright ("Use thinking.type.adaptive and output_config.effort") and rejects `budget_tokens` under adaptive. Other thinking types (`disabled`, already-`adaptive`) pass through.
   - `_apply_effort_rewrite` first **relocates** any top-level `reasoning_effort` into `output_config.effort` — Copilot no longer accepts the top-level field at all ("Extra inputs are not permitted"); `output_config.effort` is the live control surface (an existing `output_config.effort` wins if both are present). It then clamps that value to the set each model accepts. The allowed set is resolved by `_resolve_allowed_efforts` with this precedence: **live upstream `/models` capabilities** (`capabilities.supports.reasoning_effort`, cached by `ModelCapabilityCache` in `model_capabilities.py`) → the static `_EFFORT_OVERRIDES` fallback (used only when `/models` is unreachable) → the default `{low, medium, high}`. Examples (current upstream advertisement):
   - Opus 4.8 / Opus 4.7 → only `medium`; `high`/`low`/`xhigh` are downgraded to `medium`.
   - Haiku 4.5 / Opus 4.5 / Sonnet 4.5 → no `reasoning_effort` advertised, so the effort field is *removed* (not clamped).
   - Opus 4.7 `-1m-internal` → advertises `low|medium|high|xhigh`, so `xhigh` is preserved (the static table would have wrongly clamped it).
   - A non-string top-level `reasoning_effort` (can't become an `output_config.effort` string) is dropped rather than forwarded into a guaranteed 400; empty `output_config` after stripping is dropped entirely.
   Because the allowed set comes from upstream, a new or changed model needs **no code change** — the cache picks it up (TTL ~5 min, single-flight refresh, never fails an in-flight request). The effort clamp runs *after* the 1M model remap (step below) so it keys off the final model id.
3. **Build outbound headers.** `build_claude_outbound_headers` (in `headers.py`):
   - Strips RFC 7230 §6.1 hop-by-hop headers (`Connection`, `Keep-Alive`, `Proxy-Authenticate`, `Proxy-Authorization`, `TE`, `Trailer`, `Trailers`, `Transfer-Encoding`, `Upgrade`) plus `Host`, `Content-Length`.
   - **Strips inbound `Authorization`, `x-api-key`, `User-Agent`, `Cookie`, `proxy-authorization`** (any client-supplied auth or session state must NEVER reach upstream — this is a security invariant with regression tests). Upstream `Set-Cookie` is dropped on the way back.
   - Filters `anthropic-beta` for `UNSUPPORTED_BETA_TOKENS` (currently `context-1m-2025-08-07`, which Copilot rejects).
   - Sets `Authorization: Bearer <COPILOT_GITHUB_TOKEN>`, `Copilot-Integration-Id`, `Editor-Version`, `User-Agent`, `X-Request-Id`, and (case-insensitive defaulted) `anthropic-version`.
4. **Dispatch by streaming flag.**
   - **Non-streaming (`stream: false`):** `client.send(req, stream=True)` lets the proxy inspect status before buffering. For `2xx`, the full body is read and passed through with hop-by-hop headers stripped. For `>=400`, the upstream error body is read with the 32 KiB cap and returned as an Anthropic error envelope.
   - **Streaming (`stream: true`):** `_stream_response()` opens upstream with `client.send(req, stream=True)`, checks status before committing to `StreamingResponse`, then yields SSE chunks through `chunks_with_keepalive()`. See next section.
5. **Errors.** `httpx.TimeoutException` and `httpx.HTTPError` are caught and converted to Anthropic-shaped envelopes (`anthropic_json_error` for non-stream, `anthropic_sse_error_event` mid-stream).

---

## Streaming: the keepalive design

The single most subtle piece of the codebase. The naïve implementation — wrapping `await aiter_raw().__anext__()` in `anyio.move_on_after(15)` to send pings — **silently truncates streams**, because cancelling the awaited `__anext__()` finalizes the underlying async generator (PEP 525). After the first 15s quiet interval, subsequent reads return `StopAsyncIteration` and the SSE stream ends without an error frame.

`chunks_with_keepalive` solves this with a producer/consumer pattern:

```text
upstream.aiter_bytes()
        |
        v
+================+
| producer task  |
+================+
        |
        v
+===================================+
| memory_object_stream buffer size 8 |
+===================================+
        |
        v
+==========================================+
| consumer waits recv.receive() up to 15s  |
| and polls request.is_disconnected()      |
+====================+=====================+
                     |
          +==========+==========+
          |                     |
          v                     v
  chunk before timeout      no chunk before timeout
          |                     |
          v                     v
  yield upstream chunk      yield "ping" sentinel
                                |
                                v
                      Claude sends event: ping
                      Codex sends : keepalive

If the client disconnects:
  yield "disconnect" sentinel
  cancel producer
  aclose upstream response
```

Key invariants:

- **Ping timeout cancels `recv.receive()`, NOT the upstream HTTP read.** The upstream read keeps running in the background producer; cancelling a memory-channel read is harmless.
- **Producer exceptions are captured, not swallowed.** They're stashed in a `producer_error` list and re-raised by the consumer *outside* the `anyio.create_task_group()` block (so they don't get wrapped in an `ExceptionGroup`). The route-specific `body_iter` catches that and emits the right terminal SSE error frame: Anthropic-shaped for Claude, OpenAI-shaped for Codex.
- **Ping handling is route-specific.** `chunks_with_keepalive` yields a `"ping"` sentinel. Claude converts it to `event: ping`; Codex converts it to a `: keepalive` comment so each client keeps its native SSE shape.
- **Client disconnect short-circuits cleanly.** Yields a `("", "disconnect")` sentinel, cancels the task group, and `aclose`'s the upstream response in a `finally`.
- **Channel buffer = 8** keeps backpressure modest: a slow client can stall the producer after 8 buffered chunks, which is desired for memory safety.
- **Decoding:** the producer uses `aiter_bytes()` (not `aiter_raw()`), so any upstream `Content-Encoding` (gzip/br/deflate) is transparently decoded by httpx. Forwarding still-compressed bytes with the `text/event-stream` content-type would silently corrupt the stream.
- **Pre-stream errors return real HTTP status.** `_stream_response` and `_codex_stream_response` open upstream with `client.send(req, stream=True)` and inspect `upstream.status_code` *before* returning a `StreamingResponse`. If upstream answered >=400 before any body byte was forwarded, the proxy returns a `JSONResponse` with the upstream status (`401`, `429`, ...) and the route's error envelope. Only failures observed *after* the stream has started surface as terminal SSE error frames. This lets SDK retry logic that keys on HTTP status (`401` -> re-auth, `429` -> backoff) fire correctly. The bounded-read helper (`read_bounded`, 32 KiB cap) prevents a hostile or runaway upstream error body from being read into memory unbounded.

---

## Request lifecycle: `GET /claude/v1/models`

`proxy_claude_models` calls `GET {api_base}/models`, then filters and reshapes the result:

- **Vendor filter:** keeps only `vendor == "Anthropic"` (case-insensitive).
- **Capability filter:** keeps only `capabilities.type == "chat"`.
- **Picker filter:** drops models with `model_picker_enabled: false`.
- **Hide internal-only experiments without 1M context:** drops models whose name contains "internal only" *unless* the id ends in `-1m` / `-1m-internal`. The 1M-context variants happen to also be flagged "(Internal only)" by upstream — keeping them is the only way to actually get 1M context on this Copilot tenant (verified via `capabilities.limits.max_context_window_tokens=1_000_000`).
- **Canonicalize ids:** dot form -> dash form (`claude-opus-4.7` -> `claude-opus-4-7`) - *every* id, including `-1m`. Claude Code's `/model` slash command and built-in name recognition both validate against the canonical dash form, so anything we expose has to be in that shape. The dash-to-dot conversion that Copilot's `/v1/messages` requires for `-1m` ids ("model_not_supported" otherwise) is applied as a last hop in `_normalize_model_for_upstream`, so the user-facing id stays dash everywhere Claude Code touches it.
- **Defensive parsing:** non-JSON `200` bodies (captive portals, stray HTML) are caught and returned as a `502 api_error` envelope rather than 500-ing. Each model item is also `isinstance`-checked field-by-field so a malformed-but-valid-JSON shape from upstream can't `AttributeError` past the error envelope.
- **1M context routing:** see `_remap_to_1m` in `proxy_claude_messages` - when the inbound request carries `anthropic-beta: context-1m-2025-08-07` (Claude Code's hardcoded "Opus 4.7 (1M context)" picker tier) and the model is one of `claude-opus-4-7` / `claude-opus-4-6` (or their dot forms), the body's `model` is rewritten to upstream's `claude-opus-4-7-1m-internal` / `claude-opus-4-6-1m` variant (still dash; the dash-to-dot wire conversion happens in `_normalize_model_for_upstream`). The beta header itself is still stripped in `headers.py` because Copilot rejects it on every model id; the `-1m` model id is the real 1M switch. Without this remap, those picker tiers would silently downgrade to 200K context.

The response uses Anthropic's standard list envelope (`data`, `has_more`, `first_id`, `last_id`).

---

## Request lifecycle: `POST /codex/v1/responses`

Claude Code is isolated under `/claude/v1` and Codex CLI is isolated under `/codex/v1`, so their protocol-specific `/models` responses do not collide.

1. **Read body once.** `_parse_codex_request(raw_body, request_id)` returns `(rewritten_body, model, streaming, initiator)`.
2. **Rewrite only confirmed Copilot rejects.**
   - `image_generation` tools are stripped because Copilot returns `unsupported_value`.
   - `previous_response_id` is stripped because Copilot returns `previous_response_id is not supported`.
   - `web_search` is preserved because live probes showed this tenant accepts it.
   - When a rewrite happens, the body is re-serialized with `ensure_ascii=False`; otherwise the original bytes are forwarded.
3. **Build Codex/Responses headers.** `build_codex_outbound_headers` rebuilds from proxy settings instead of forwarding Codex local session headers. It injects `Authorization`, `Copilot-Integration-Id`, `Editor-Version`, `Editor-Plugin-Version`, `User-Agent`, `OpenAI-Intent: conversation-panel`, `X-Interaction-Type: conversation-panel`, `X-GitHub-Api-Version`, `X-VSCode-User-Agent-Library-Version: electron-fetch`, stable process-lifetime `VScode-SessionId` and `VScode-MachineId`, `X-Initiator`, and per-request `X-Request-Id`.
4. **Forward to Copilot's native Responses endpoint.** The upstream URL is `{COPILOT_API_BASE}/responses`, not `/v1/responses`.
5. **Streaming.** Codex streams are SSE passthrough with the same producer/consumer keepalive engine used by Claude streams. The Codex path emits comment keepalives (`: keepalive`) and OpenAI-shaped terminal error frames instead of Anthropic `event: ping` / Anthropic error envelopes.
6. **Errors.** Codex routes return OpenAI-shaped JSON errors: `{"error":{"message":..., "type":..., "param":..., "code":...}}`.

`POST /codex/v1/responses/compact` first tries `{COPILOT_API_BASE}/responses/compact`. Copilot currently returns 404, so the proxy appends a checkpoint-compaction prompt to the input, calls normal `/responses` with `stream: false` and `store: false`, and wraps the result as `object: "response.compaction"`.

`GET /codex/v1/models` calls `{COPILOT_API_BASE}/models`, keeps models whose `supported_endpoints` include `/responses`, and returns both the OpenAI list envelope (`object: "list"`, `data: [...]`) and Codex's model catalog envelope (`models: [...]`) for compatibility with Codex model discovery. The catalog entries include the fields current Codex CLI deserializes as model metadata, including base instructions, reasoning-summary flags, verbosity support, truncation policy, parallel-tool-call support, context-window hints, input modalities, and search-tool support.

---

## Configuration

Runtime config comes from environment variables. `config.py:Settings.from_env()` parses the proxy settings used by request handlers; `__main__.py` reads `COPILOT_PROXY_HOST` only for the rarely used `python -m copilot_cli_relay` debug entrypoint. The settings dataclass is frozen; tests inject via `reset_settings_for_tests()` rather than mutating env.

<table>
  <thead>
    <tr>
      <th width="320">Environment variable</th>
      <th>Default</th>
      <th>Purpose</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td width="320"><code>COPILOT_GITHUB_TOKEN</code></td>
      <td>-</td>
      <td><strong>Required.</strong> Must start <code>gho_</code> or <code>ghu_</code>; rejects PATs (<code>ghp_</code>) early with a clear message.</td>
    </tr>
    <tr>
      <td width="320"><code>COPILOT_PROXY_PORT</code></td>
      <td><code>4141</code></td>
      <td>uvicorn listen port for the <code>python -m copilot_cli_relay</code> entrypoint. The Docker <code>CMD</code> hardcodes <code>--port 4141</code>, so this env var has no effect when running via <code>docker compose</code>. Change the published port in <code>docker-compose.yml</code> instead.</td>
    </tr>
    <tr>
      <td width="320"><code>COPILOT_PROXY_HOST</code></td>
      <td><code>127.0.0.1</code></td>
      <td>Bind interface used only by the <code>python -m copilot_cli_relay</code> entrypoint. The Docker entrypoint passes <code>--host 0.0.0.0</code> inside the container; host loopback binding is enforced by <code>docker-compose.yml</code>.</td>
    </tr>
    <tr>
      <td width="320"><code>COPILOT_API_BASE</code></td>
      <td><code>https://api.githubcopilot.com</code></td>
      <td>Override upstream for mocks or tests. Must use <code>https://</code> unless <code>COPILOT_ALLOW_INSECURE_API_BASE=1</code> is set, because the proxy injects the OAuth bearer into every outbound request.</td>
    </tr>
    <tr>
      <td width="320"><code>COPILOT_ALLOW_INSECURE_API_BASE</code></td>
      <td><code>0</code></td>
      <td>Set to <code>1</code> to allow an <code>http://</code> <code>COPILOT_API_BASE</code> for test mocks. Never use this in production.</td>
    </tr>
    <tr>
      <td width="320"><code>COPILOT_INTEGRATION_ID</code></td>
      <td><code>copilot-developer-cli</code></td>
      <td><strong>Do not change</strong> unless you know your tenant requires a different value.</td>
    </tr>
    <tr>
      <td width="320"><code>COPILOT_EDITOR_VERSION</code></td>
      <td><code>copilot-cli-relay/0.3.0</code></td>
      <td>Sent as <code>Editor-Version</code> and <code>User-Agent</code>.</td>
    </tr>
    <tr>
      <td width="320"><code>COPILOT_CODEX_INTEGRATION_ID</code></td>
      <td>value of <code>COPILOT_INTEGRATION_ID</code></td>
      <td>Sent only on Codex/Responses routes; live testing showed <code>copilot-developer-cli</code> works for <code>gpt-5.5</code> and <code>vscode-chat</code> does not on this tenant.</td>
    </tr>
    <tr>
      <td width="320"><code>COPILOT_CODEX_EDITOR_VERSION</code></td>
      <td><code>vscode/1.99.0</code></td>
      <td>VS Code-style editor version for Responses requests.</td>
    </tr>
    <tr>
      <td width="320"><code>COPILOT_CODEX_PLUGIN_VERSION</code></td>
      <td><code>copilot-chat/0.43.2026033101</code></td>
      <td>Copilot Chat plugin version for Responses requests.</td>
    </tr>
    <tr>
      <td width="320"><code>COPILOT_CODEX_USER_AGENT</code></td>
      <td><code>GitHubCopilotChat/0.43.2026033101</code></td>
      <td>Responses user agent.</td>
    </tr>
    <tr>
      <td width="320"><code>COPILOT_CODEX_GITHUB_API_VERSION</code></td>
      <td><code>2026-01-09</code></td>
      <td><code>X-GitHub-Api-Version</code> for Responses requests.</td>
    </tr>
    <tr>
      <td width="320"><code>COPILOT_CODEX_SESSION_ID</code></td>
      <td>generated per process</td>
      <td>Optional stable VS Code session id override.</td>
    </tr>
    <tr>
      <td width="320"><code>COPILOT_CODEX_MACHINE_ID</code></td>
      <td>generated per process</td>
      <td>Optional stable VS Code machine id override.</td>
    </tr>
    <tr>
      <td width="320"><code>LOG_LEVEL</code></td>
      <td><code>info</code></td>
      <td><code>debug</code> enables header-level debug logging.</td>
    </tr>
    <tr>
      <td width="320"><code>LOG_BODIES</code></td>
      <td><code>0</code></td>
      <td><code>1</code> enables redacted body logging. Bodies contain the user's source code.</td>
    </tr>
  </tbody>
</table>

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
- **Browser cross-site guard:** `server.py` installs `security.LoopbackHostMiddleware` for `localhost`, `127.0.0.1`, and `[::1]` only, then `security.LocalBrowserGuardMiddleware` rejects non-loopback `Origin` headers, `Sec-Fetch-Site: cross-site`, and non-JSON POSTs to `/claude/v1/messages`, `/codex/v1/responses`, and `/codex/v1/responses/compact`. This prevents DNS rebinding and CORS-simple POSTs from driving the relay with the user's Copilot token.
- **No client-supplied auth ever reaches upstream.** `Authorization`, `x-api-key`, `proxy-authorization`, `Cookie`, and `User-Agent` are stripped in `headers.build_claude_outbound_headers`; regression-tested. Upstream `Set-Cookie` is similarly dropped from responses returned to the client.
- **Codex local session headers do not reach upstream.** The Codex path rebuilds headers from settings and drops Codex-specific local headers such as `x-codex-turn-metadata`, `session_id`, and `thread_id`.
- **Token storage:** read fresh from `.env` at container start; never persisted by the proxy. The `extract-token.ps1` helper writes `.env` with a hardened ACL — inheritance disabled and dropped, then explicit ACEs for the current user and `NT AUTHORITY\SYSTEM`. In practice, the resulting on-disk DACL also includes `BUILTIN\Administrators` with FullControl when the script is run by an account in that group; the script does not strip this. The script *fails closed* if the ACL operation can't be applied — it refuses to leave a token on disk under default permissions.
- **Bodies off by default.** `LOG_BODIES=0`. When enabled, redaction runs on every body.
- **Container runs non-root** (UID 1000).
- **`.gitignore` and `.dockerignore`** both block `.env` and `.env.*` while allowing `.env.example`. Verified with `git check-ignore`.
- **Upstream errors echo at most 300 chars** of the upstream body in the error envelope. The hop-by-hop strip prevents `Authorization` from being reflected back if upstream ever did so.

---

## Testing

Tests live in `tests/unit/` and run inside the same Docker image as production. **There is no host-side Python or `venv` involved** — the README's "Development" section explains the full Docker-only workflow, including how to add or upgrade dependencies via throwaway containers.

```powershell
docker compose run --rm proxy uv run pytest tests/unit -q
```

For coverage:

```powershell
docker compose run --rm proxy sh -lc "uv run coverage run --source=copilot_cli_relay -m pytest tests/unit -q && uv run coverage report --show-missing"
```

Current state: 212 tests with 99% package coverage. Coverage stays concentrated around request rewriting, header safety, streaming behavior, model filtering, and Codex model-catalog compatibility.

Coverage hot-spots that matter:
- `chunks_with_keepalive` - both happy-path (chunks-after-pings regression) and producer-exception propagation are covered.
- Inbound auth-leak - explicit test asserts `Authorization`, `x-api-key`, `proxy-authorization`, `Cookie` never reach upstream.
- Body redaction - every pattern has a positive test.
- Hop-by-hop headers - both `Trailer` (singular) and `Trailers` are covered on inbound and outbound.
- Codex routes - non-streaming, streaming, compact fallback, model catalog, health, malformed input, upstream error-body failures, and OpenAI SSE error shape are covered.

---

## Don't touch

- **`Copilot-Integration-Id` default.** It's specifically the value that unlocks Claude and GPT-5 Responses models on this tenant.
- **Streaming/keepalive structure.** It looks over-engineered; it isn't. See the keepalive section above.
- **`/claude/v1/models` filtering rules.** Each rule is there because Claude Code misbehaves without it.
- **`/codex/v1` namespace and model catalog shape.** Codex needs OpenAI Responses routes and a Codex-compatible `models` catalog without colliding with Claude Code's Anthropic `/claude/v1/models` response.
- **`gho_`/`ghu_` prefix check in `config.py`.** It catches the common mistake of pasting a PAT (`ghp_`).
- **Strict `--frozen` in the Dockerfile.** It exists to fail-fast on lockfile drift; do not add a `|| uv lock` fallback.
