# Architecture

`copilot-cli-relay` is a thin reverse proxy for Claude Code, Codex CLI, and
**Codex in the unified ChatGPT desktop app**, backed by GitHub Copilot. Claude
uses native Anthropic Messages; both Codex clients use native OpenAI Responses.
The relay adapts authentication, headers, and selected request fields rather
than translating between API protocols.

This file is the contract for anyone modifying the proxy: what each piece does, where the non-obvious behavior lives, and what *must not* break.

The app integration covers local Codex model traffic, not ChatGPT Chat/Work,
cloud execution, or all app services. The [app guide](docs/codex-desktop.md)
contains setup and dated validation; the [glossary](GLOSSARY.md) distinguishes
the unified app from the historical "Codex Desktop" name. Existing `/codex/v1`
routes, provider IDs, helper filenames, and the tested `OpenAI.Codex` package
identifier remain unchanged.

---

## High-level data flow

```text
+==============================+       +========================================+       +================================+
| Claude Code                  |       | copilot-cli-relay                      |       | GitHub Copilot                 |
| Anthropic Messages JSON/SSE  |       | 127.0.0.1:4141                         |       | Anthropic native endpoints     |
| POST /claude/v1/messages     | ====> | Claude route handlers                  | ====> | POST /v1/messages              |
| GET  /claude/v1/models       | <==== | build_claude_outbound_headers          | <==== | GET  /models                   |
+==============================+       |                                        |       +================================+
                                       | uvicorn + Starlette                    |
                                       | shared httpx AsyncClient, HTTP/2       |
                                       | proxy_shared streaming + passthrough   |
+==============================+       |                                        |       +================================+
| Codex CLI / ChatGPT app      |       | Codex route handlers                   |       | GitHub Copilot                 |
| (Codex, local)               |       |                                        |       |                                |
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
4. `Copilot-Integration-Id: copilot-developer-cli` is the tested integration identity for both routes. Other integration IDs can expose different catalogs; do not change this as part of app setup.

The ChatGPT app's bundled Codex engine owns the conversation, full-history
replay, and local tool execution. The relay forwards model requests and results;
it neither runs those tools nor hosts the app's conversation state.

---

## Routes

| Method & path | Handler | Purpose |
|---|---|---|
| `POST /claude/v1/messages` | `proxy_claude_messages` | Forward Claude/Anthropic request; supports streaming and non-streaming |
| `GET  /claude/v1/models` | `proxy_claude_models` | Fetch upstream models, filter to Anthropic chat-eligible, canonicalize ids to dash form |
| `POST /codex/v1/responses` | `proxy_codex_responses` | Forward CLI and local app Codex requests to Copilot `/responses` |
| `POST /codex/v1/responses/compact` | `proxy_codex_responses_compact` | Try native Responses compaction, then synthesize compaction if upstream 404s |
| `GET  /codex/v1/models` | `proxy_codex_models` | Fetch upstream models, filter to `/responses`-capable models, return both OpenAI list and Codex catalog shapes |
| `GET  /claude/healthz` | `claude_healthz` | Liveness + upstream reachability + Anthropic model count |
| `GET  /codex/healthz` | `codex_healthz` | Liveness + upstream reachability + Responses model count |
| `GET  /healthz` | `claude_healthz` | Backward-compatible alias for `/claude/healthz` |

All routes are registered in [`server.py`](src/copilot_cli_relay/server.py).
Its lifespan owns one shared `httpx.AsyncClient(http2=True, trust_env=False)`
and one `ModelCapabilityCache`. Shutdown closes the capability cache and its
refresh task before closing the HTTP client. There is no separate app server,
desktop-only inference endpoint, or WebSocket route.

Health endpoints return HTTP 200 even when `upstream_ok` is false. Read
`upstream_ok`, `upstream_status`, and the route's model count to assess upstream
health. The Compose healthcheck only tests the Claude endpoint's HTTP status;
its `healthy` label is not a Codex readiness or inference result.

The upstream catalog must be an object containing a `data` array. Invalid
containers produce protocol-shaped 502 responses from model routes and
`upstream_ok=false` from health routes. Individual rows retain the existing
tolerant filtering/defaults. Health counts advertised matching rows, not
deduplicated selectable models: Anthropic-vendor rows for Claude and
Responses-advertising rows for Codex. An empty valid list can be upstream-healthy
with count zero; the app helper separately checks both catalogs and its model.

---

## File map

```text
src/copilot_cli_relay/
|-- __init__.py           # Package description and version
|-- __main__.py           # Optional module entrypoint, still run inside Docker
|-- server.py             # Starlette routes, shared HTTP client, capability cache
|-- config.py             # Immutable Settings and OAuth-token validation
|-- headers.py            # Protocol-specific outbound header construction
|-- claude_proxy.py       # Anthropic Messages handlers
|-- codex_proxy.py        # Responses handlers shared by CLI and app clients
|-- model_capabilities.py # Live Claude effort-capability cache
|-- security.py           # Loopback-host and browser cross-site guards
|-- proxy_shared.py       # Streaming ownership, framing, bounded error reads
|-- errors.py             # Protocol-specific JSON and SSE errors
`-- logging_setup.py      # Logging and best-effort secret redaction

scripts/
|-- extract-token.ps1     # Credential bootstrap into the ignored local .env
|-- claude-config.ps1     # Print or explicitly write Claude settings
|-- codex-config.ps1      # Print or explicitly replace managed CLI config
|-- codex-desktop.ps1     # Print/check only: Codex in the ChatGPT app
`-- proxy.ps1             # Interactive container lifecycle and route checks

tests/unit/              # Python relay tests, including stream lifetimes
tests/powershell/        # Dependency-free Windows app-helper tests
docs/codex-desktop.md    # App configuration, evidence, and compatibility limits
GLOSSARY.md              # Client and routing terminology
```

`Dockerfile`, `docker-compose.yml`, `pyproject.toml`, and `uv.lock` define the
Docker-only Python runtime. The standalone `skill/claude-model-switch/` helper
is Claude-specific; it does not configure Codex in either client.

---

## Request lifecycle: `POST /claude/v1/messages`

1. **Read body once.** `_parse_claude_request(raw_body, caps)` performs a single `json.loads` and returns `(rewritten_body, model, streaming)`. Malformed bodies pass through unchanged so the client sees Copilot's actual error message.
2. **Rewrite the body for Copilot.** Two massaging steps run before forwarding:
   - `_apply_thinking_rewrite` converts `thinking: {type: "enabled", budget_tokens: N}` (the legacy extended-thinking shape Claude Code sends) into `thinking: {type: "adaptive"}` and drops `budget_tokens`. Copilot's Anthropic endpoint now rejects `thinking.type.enabled` outright ("Use thinking.type.adaptive and output_config.effort") and rejects `budget_tokens` under adaptive. Other thinking types (`disabled`, already-`adaptive`) pass through.
   - `_apply_effort_rewrite` first **relocates** any top-level `reasoning_effort` into `output_config.effort` — Copilot no longer accepts the top-level field at all ("Extra inputs are not permitted"); `output_config.effort` is the live control surface (an existing `output_config.effort` wins if both are present). It then clamps that value to the set each model accepts. The allowed set is resolved by `_resolve_allowed_efforts` with this precedence: **live upstream `/models` capabilities** (`capabilities.supports.reasoning_effort`, cached by `ModelCapabilityCache` in `model_capabilities.py`) → the static `_EFFORT_OVERRIDES` fallback (used only when `/models` is unreachable) → the default `{low, medium, high}`. Examples (current upstream advertisement):
   - Current Opus and Sonnet models advertise `low|medium|high|max`; Opus 4.7, Opus 4.8, Opus 5, and Sonnet 5 also advertise `xhigh`.
   - Haiku 4.5 does not advertise `reasoning_effort`, so the effort field is *removed* (not clamped).
   - A non-string top-level `reasoning_effort` (can't become an `output_config.effort` string) is dropped rather than forwarded into a guaranteed 400; empty `output_config` after stripping is dropped entirely.
   Because the allowed set comes from upstream, a new or changed model needs **no code change** — the cache picks it up (TTL ~5 min, single-flight refresh, never fails an in-flight request).
3. **Build outbound headers.** `build_claude_outbound_headers` (in `headers.py`):
   - Strips RFC 7230 §6.1 hop-by-hop headers (`Connection`, `Keep-Alive`, `Proxy-Authenticate`, `Proxy-Authorization`, `TE`, `Trailer`, `Trailers`, `Transfer-Encoding`, `Upgrade`) plus `Host`, `Content-Length`.
   - **Strips inbound `Authorization`, `x-api-key`, `User-Agent`, `Cookie`, `proxy-authorization`** (any client-supplied auth or session state must NEVER reach upstream — this is a security invariant with regression tests). Upstream `Set-Cookie` is dropped on the way back.
   - Strips all incoming casing variants of relay-owned `Copilot-Integration-Id`, `Editor-Version`, and `X-Request-Id` before injection, so each outbound identity/correlation header has one authoritative value.
   - Filters `anthropic-beta` through `_filter_anthropic_beta`, removing the `UNSUPPORTED_BETA_TOKENS` case-insensitively. The current set is `context-1m-2025-08-07` and `advisor-tool-2026-03-01`.
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
  track event boundary      at a complete event boundary?
  yield upstream chunk      yes: yield "ping"; no: keep waiting
                                |
                                v
                      Claude sends event: ping
                      Codex sends : keepalive

If the client disconnects:
  cancel and join producer; close both channel endpoints
  yield "disconnect" sentinel
  route closes iterator; response owner closes upstream
```

Key invariants:

- **Ping timeout cancels `recv.receive()`, NOT the upstream HTTP read.** The upstream read keeps running in the background producer; cancelling a memory-channel read is harmless.
- **Frame-safe heartbeats.** Constant-size parser state tracks complete SSE events without accumulating their payloads. A pause inside a partial field/event, including an ambiguous CRLF split, deliberately gets no heartbeat. Upstream chunks are not rewritten.
- **Producer exceptions are captured, not swallowed.** They are stashed in a `producer_error` list and re-raised after producer cleanup, without exception-group wrapping. The route-specific `body_iter` emits the protocol's terminal error frame; a truncated event cannot be repaired retroactively.
- **Ping handling is route-specific.** `chunks_with_keepalive` yields a `"ping"` sentinel. Claude converts it to `event: ping`; Codex converts it to a `: keepalive` comment so each client keeps its native SSE shape.
- **Client disconnect short-circuits cleanly.** The consumer cancels and joins its producer before yielding a terminal disconnect sentinel. Iterator and upstream-response closure are protected by explicit ownership and cancellation-shielded cleanup.
- **Channel buffer = 8** bounds queued chunks and applies backpressure to a slow client. It does not impose a byte limit on each upstream chunk.
- **Decoding:** the producer uses `aiter_bytes()` (not `aiter_raw()`), so any upstream `Content-Encoding` (gzip/br/deflate) is transparently decoded by httpx. Forwarding still-compressed bytes with the `text/event-stream` content-type would silently corrupt the stream.
- **Pre-stream errors return real HTTP status.** `_stream_response` and `_codex_stream_response` open upstream with `client.send(req, stream=True)` and inspect `upstream.status_code` *before* returning a `StreamingResponse`. If upstream answered >=400 before any body byte was forwarded, the proxy returns a `JSONResponse` with the upstream status (`401`, `429`, ...) and the route's error envelope. Only failures observed *after* the stream has started surface as terminal SSE error frames. This lets SDK retry logic that keys on HTTP status (`401` -> re-auth, `429` -> backoff) fire correctly. The bounded-read helper (`read_bounded`, 32 KiB cap) prevents a hostile or runaway upstream error body from being read into memory unbounded.

### Stream ownership and cancellation

The producer is an explicitly owned asyncio task, not an AnyIO task-group
cancel scope suspended across async-generator yields. The consumer cancels and
joins it and closes both memory-channel endpoints, even if the producer never
started. The routes consume it with `aclosing`.

`UpstreamStreamingResponse` owns both the response-body iterator and the acquired
upstream response. It closes them when downstream header/body transmission
fails, the body ends, or the request is canceled. `_CancellationSafeStream`
protects the transport's first close, including HTTPX's automatic EOF close:
HTTPX sets `response.is_closed` before awaiting transport cleanup, so retrying
`response.aclose()` alone cannot establish that cleanup completed.

Cleanup waits use AnyIO shielding and repeated `asyncio.shield` waits. Caller
cancellation is re-raised only after cleanup finishes. Cleanup failures are
returned internally as sanitized values so Python's shielded-future exception
logging cannot expose raw exception text. Producer read failures retain their
original exception types; cleanup failures become sanitized errors.

Do not replace the shielded producer join with an unshielded await that simply
suppresses `CancelledError`. Do not claim that these lifetimes repair an
already-forwarded partial JSON event or promptly detect a downstream socket
disconnect while still awaiting upstream headers. Those are separate limits.

---

## Request lifecycle: `GET /claude/v1/models`

`proxy_claude_models` calls `GET {api_base}/models`, then filters and reshapes the result:

- **Vendor filter:** keeps only `vendor == "Anthropic"` (case-insensitive).
- **Capability filter:** keeps only `capabilities.type == "chat"`.
- **Picker filter:** drops models with `model_picker_enabled: false`.
- **Hide internal-only experiments:** drops models whose name contains "internal only". Standard picker models now advertise their full context windows directly.
- **Canonicalize ids:** dot form -> dash form (`claude-opus-4.7` -> `claude-opus-4-7`). Claude Code's `/model` slash command and built-in name recognition validate against the canonical dash form.
- **Defensive parsing:** non-JSON bodies and invalid outer catalog/data shapes return a `502 api_error`. Malformed individual fields are skipped or defaulted as appropriate; non-string vendor values are excluded safely. Health success is assigned only after parsing and counting finish. See the [regression record](docs/project-review-2026-10-18.md).
- **1M context:** standard Opus and Sonnet models advertise 1M context directly. The obsolete `context-1m-2025-08-07` beta token is stripped because Copilot rejects it. Previously documented internal `-1m` ids are translated to their standard replacements for configuration compatibility.

The response uses Anthropic's standard list envelope (`data`, `has_more`, `first_id`, `last_id`).

---

## Request lifecycle: `POST /codex/v1/responses`

Claude Code is isolated under `/claude/v1`; Codex CLI and Codex in the ChatGPT
app share `/codex/v1`. Their protocol-specific model catalogs do not collide.

1. **Read body once.** `_parse_codex_request(raw_body, request_id)` returns `(rewritten_body, model, streaming, initiator)`.
2. **Rewrite only confirmed Copilot rejects.**
   - `image_generation` tools are stripped because Copilot returns `unsupported_value`.
   - A `tool_choice` targeting a removed tool is cleared. `"required"` is also removed if stripping leaves no tools.
   - `previous_response_id` is stripped because Copilot returns `previous_response_id is not supported`.
   - `web_search` is preserved because live probes showed this tenant accepts it.
   - When a rewrite happens, the body is re-serialized with `ensure_ascii=False`; otherwise the original bytes are forwarded.
3. **Build Codex/Responses headers.** `build_codex_outbound_headers` rebuilds from proxy settings instead of forwarding Codex local session headers. It injects `Authorization`, `Copilot-Integration-Id`, `Editor-Version`, `Editor-Plugin-Version`, `User-Agent`, `OpenAI-Intent: conversation-panel`, `X-Interaction-Type: conversation-panel`, `X-GitHub-Api-Version`, `X-VSCode-User-Agent-Library-Version: electron-fetch`, stable process-lifetime `VScode-SessionId` and `VScode-MachineId`, `X-Initiator`, and per-request `X-Request-Id`.
4. **Forward to Copilot's native Responses endpoint.** The upstream URL is `{COPILOT_API_BASE}/responses`, not `/v1/responses`.
5. **Streaming.** Codex streams are SSE passthrough with the same producer/consumer keepalive engine used by Claude streams. The Codex path emits comment keepalives (`: keepalive`) and OpenAI-shaped terminal error frames instead of Anthropic `event: ping` / Anthropic error envelopes.
6. **Errors.** Codex routes return OpenAI-shaped JSON errors: `{"error":{"message":..., "type":..., "param":..., "code":...}}`.

Full-history replay preserves function/custom tool calls and matching result
items, including their call IDs. The initiator classifier recognizes those
continuations instead of treating them as new user input. The relay has no
incremental conversation store: clients must replay the required history rather
than depend on the stripped `previous_response_id`.

`POST /codex/v1/responses/compact` first tries the native upstream compact
endpoint. On 404 only, it appends a summary prompt, calls ordinary `/responses`
with `stream: false` and `store: false`, and wraps the result as
`object: "response.compaction"` only after validating a completed Response and
completed assistant messages with nonblank `output_text`. Failed, incomplete,
empty, refusal-only, tool-only, or malformed output returns a static
OpenAI-shaped 502 instead of fabricated success. Known nested reasoning,
annotation, logprob, and usage fields are validated before forwarding.
Non-finite JSON numbers, numeric overflow, and serialization failures also
return static 502 errors rather than escaping as plain-text 500s.
Native responses and upstream
HTTP errors retain their existing passthrough/status behavior. This fallback is not qualified as a safe
replacement checkpoint or v2 compaction for long app conversations.

`GET /codex/v1/models` filters upstream models for `/responses` support and
returns both the OpenAI list (`object`, `data`) and Codex catalog (`models`).
Catalog entries include reasoning, tool, vision, verbosity, truncation, and
context metadata, using upstream capabilities and relay defaults. Metadata
guides client behavior; it does not create upstream capabilities. Validate the
actual model against the app's bundled engine, not just a separately installed
CLI version. Command-backed dummy auth is the tested discovery setup in both
helpers; the app helper also supplies an explicit catalog URL.

---

<a id="desktop-integration-and-stream-ownership"></a>

## ChatGPT desktop app integration

The local Codex path is:

```text
ChatGPT app (Codex, This computer)
  -> bundled Codex engine + selected relay provider
  -> HTTP/SSE at 127.0.0.1:4141/codex/v1
  -> existing Responses handlers
  -> GitHub Copilot
```

`scripts/codex-desktop.ps1` has two modes:

| Mode | Reads or produces | Does not establish |
|---|---|---|
| Default | Prints root model/provider keys, a separate provider, catalog URL, and absolute-path dummy-auth command | Which configuration is active in the app |
| `-Check` | Reads relay health and both model catalogs; checks the selected model and installed `OpenAI.Codex` package/bundled engine | Inference, GUI streaming, tools, follow-up, or recovery |

The helper has no write, migration, installation, app-launch, or permission
mode. It preserves the existing CLI setup by not invoking the CLI writer.
Its printed defaults select HTTP/SSE with `supports_websockets = false`.

After a deliberate merge, the app's Codex engine reads the shared user
configuration, subject to configuration precedence and managed requirements.
Root provider changes can affect unprofiled CLI launches. A CLI `--profile`
does not select that profile in the app; an effective `model_catalog_json`
overrides the remote catalog URL. Saved threads may reference provider IDs, so
renaming the product is not a reason to remove an existing provider.

The Windows pilot has distinct evidence for the original GUI tool loop, the
updated relay's bundled-engine tool/resume tests, and a user-confirmed
post-restart app response. GUI follow-up, cancellation/recovery, and reopening
remain unverified. See the [qualification record](docs/codex-desktop.md#recorded-validation)
before claiming another model, app build, OS, Worktree mode, or cloud feature
is supported.

The [official app settings](https://learn.chatgpt.com/docs/developer-settings)
and [gateway configuration](https://learn.chatgpt.com/docs/enterprise/connect-to-a-gateway)
describe the shared configuration mechanism, not an endorsement of this
unofficial Copilot integration.

## Configuration

Runtime config comes from environment variables. `config.py:Settings.from_env()` parses the proxy settings used by request handlers; `__main__.py` reads `COPILOT_PROXY_HOST` only for the rarely used `python -m copilot_cli_relay` debug entrypoint. The settings dataclass is frozen; tests inject via `reset_settings_for_tests()` rather than mutating env.

Compose injects `.env` values when creating the container. Restart picks up
bind-mounted source changes, but changing a token or another `.env` value
requires container recreation. App `config.toml` belongs to the client, not to
this container; save work and restart the app after changing its provider.
The `COPILOT_CODEX_*` settings below apply to both Codex clients.

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
      <td><code>copilot-cli-relay/0.5.0</code></td>
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

`redact_bytes`:
- Checks raw input size before any redaction or data-URI shortening. Bodies at
  or above 16 KiB are omitted for logging. Error builders explicitly use the
  bounded reader's 32 KiB cap instead.
- Treats exact-cap input as potentially truncated, without reading another
  upstream chunk to disambiguate EOF.
- Redacts complete in-limit text before shortening data URIs or truncating an
  expanded redacted result for display.
- Returns a static omission marker if redaction fails, never the original body
  or redactor exception text.

The token prefix (e.g. `gho_`) is preserved in redaction output to aid debugging without revealing the secret.

The [truncation finding](docs/project-review-2026-10-18.md#1-truncation-can-defeat-field-based-secret-redaction)
is fixed for relay-imposed limits. Pattern-based redaction still cannot promise
protection for arbitrary malformed or already-truncated upstream text below
the limit, or unknown secret formats. Do not share diagnostics automatically.

---

## Security model

- **Bind:** `127.0.0.1:4141` only. The Docker port-publish enforces this; the `__main__` entrypoint also defaults to it.
- **Browser cross-site guard:** `server.py` installs `security.LoopbackHostMiddleware` for `localhost`, `127.0.0.1`, and `[::1]` only, then `security.LocalBrowserGuardMiddleware` rejects non-loopback `Origin` headers, `Sec-Fetch-Site: cross-site`, and non-JSON POSTs to `/claude/v1/messages`, `/codex/v1/responses`, and `/codex/v1/responses/compact`. This prevents DNS rebinding and CORS-simple POSTs from driving the relay with the user's Copilot token.
- **No client-supplied auth ever reaches upstream.** `Authorization`, `x-api-key`, `proxy-authorization`, `Cookie`, and `User-Agent` are stripped in `headers.build_claude_outbound_headers`; regression-tested. Upstream `Set-Cookie` is similarly dropped from responses returned to the client.
- **Codex local session headers do not reach upstream.** The Codex path rebuilds headers from settings and drops Codex-specific local headers such as `x-codex-turn-metadata`, `session_id`, and `thread_id`.
- **Token storage:** Compose loads `.env` at container creation; the proxy reads the injected environment at process startup and never persists the token. The extraction helper hardens its temporary file's ACL before writing token bytes, then replaces `.env`. Explicit user and SYSTEM access is retained; local Administrators can also have access as noted in the README. ACL failure stops the write.
- **Local trust boundary:** the dummy client token is not authentication for other users. Keep the relay on loopback; it is not a multi-user gateway. A local process able to reach it can use the configured Copilot identity.
- **App execution is separate:** local commands run in the client's execution environment, not this container. Model requests can include source excerpts and tool results. MCP, plugins, browser access, and ChatGPT services have separate connections and permissions; routing a model does not grant those permissions.
- **Bodies off by default.** `LOG_BODIES=0`. When enabled, redaction runs on every body.
- **Container runs non-root** (UID 1000).
- **`.gitignore` and `.dockerignore`** both block `.env` and `.env.*` while allowing `.env.example`. Verified with `git check-ignore`.
- **Upstream error details are bounded, not guaranteed secret-free.** Capped bodies are omitted; in-limit bodies are redacted before the final display slice. Header stripping is not a substitute for body redaction, and diagnostics remain sensitive.

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

Verified baseline: **478 Python tests, 99% package coverage**, plus **38
PowerShell assertions** for the app helper. `proxy_shared.py` has 100% coverage;
remaining uncovered lines are in the retired-Claude-model compatibility helper. These numbers
describe automated coverage, not blanket app compatibility.

```powershell
# Existing PowerShell 7, synthetic inputs and mocked fetches; no extra modules
pwsh tests\powershell\test-codex-desktop.ps1
```

Coverage hot-spots that matter:
- `chunks_with_keepalive` - both happy-path (chunks-after-pings regression) and producer-exception propagation are covered.
- Inbound auth-leak - explicit test asserts `Authorization`, `x-api-key`, `proxy-authorization`, `Cookie` never reach upstream.
- Body redaction - every pattern has a positive test.
- Hop-by-hop headers - both `Trailer` (singular) and `Trailers` are covered on inbound and outbound.
- Codex routes - non-streaming, streaming, compact fallback, model catalog, health, malformed input, upstream error-body failures, and OpenAI SSE error shape are covered.
- Stream lifetimes - fragmented events, early close, blocked producers, header/body send failure, repeated cancellation, and one-shot transport cleanup.
- Tool replay - function/custom call and result preservation, initiator classification, and invalid tool-choice cleanup.
- App helper - loopback-only URL validation, reserved provider IDs, TOML escaping, one resolved auth executable, both catalog shapes, and fail-closed prerequisite checks.
- Runtime hardening - `test_runtime_hardening.py` covers diagnostic size boundaries, unread error tails, completed synthetic summaries, malformed catalogs/health, and case-insensitive identity-header collisions.

The [app guide](docs/codex-desktop.md#recorded-validation) keeps real GUI evidence
separate from bundled-engine tests and unit tests. Do not count a model-list
request or a text-only reply as a tool/recovery qualification.

---

## Don't touch

- **`Copilot-Integration-Id` default.** It's specifically the value that unlocks Claude and GPT-5 Responses models on this tenant.
- **Streaming/keepalive structure.** It looks over-engineered; it isn't. See the keepalive section above.
- **`/claude/v1/models` filtering rules.** Each rule is there because Claude Code misbehaves without it.
- **`/codex/v1` namespace and model catalog shape.** Codex needs OpenAI Responses routes and a Codex-compatible `models` catalog without colliding with Claude Code's Anthropic `/claude/v1/models` response.
- **App-helper boundaries.** Keep `codex-desktop.ps1` print/check-only. Do not add credential reads, automatic configuration migration, app installation, permission changes, or provider renaming as incidental setup behavior.
- **`gho_`/`ghu_` prefix check in `config.py`.** It catches the common mistake of pasting a PAT (`ghp_`).
- **Strict `--frozen` in the Dockerfile.** It exists to fail-fast on lockfile drift; do not add a `|| uv lock` fallback.
