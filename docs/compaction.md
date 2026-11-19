# Harness-managed compaction

Use `/compact` inside Codex CLI or a local Codex conversation in the ChatGPT
app. The user reported successful manual compaction in both clients on
October 26, 2026. The supported setup selects the harness's local compaction
path, not the standalone `/responses/compact` API.

Here, "local" means the harness manages the summary and conversation history.
It does not mean offline inference: summarization can still send an ordinary
Responses request through the selected relay provider.

## Configuration

Both setup helpers generate this assignment inside the selected provider table,
before its `.auth` table:

```toml
[model_providers.copilot_desktop_pilot]
# Keep the existing name, URLs, model selection, transport, and auth settings.
capabilities.remote_compaction = "unsupported"
```

Use your existing provider ID. The CLI helper uses `copilot`; new desktop setups
use `copilot_desktop`. Existing `copilot_desktop_pilot` IDs remain valid and must
not be renamed merely to change compaction behavior.

This is an addition to the existing table, not a replacement configuration or a
second table to append. If that provider already has a `.capabilities` table,
set `remote_compaction = "unsupported"` there instead. Replace an existing value
rather than duplicating it. Preserve other capabilities.

The [official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference),
consulted October 26, 2026, defines `unsupported` as local compaction and `v2` as
remote compaction triggers through Responses. This setting selects the
compaction implementation; it does **not** disable automatic context-limit
compaction. Do not use an inflated token threshold to suppress automatic
compaction. A deliberately earlier threshold is a separate, optional mitigation
for request-byte growth, described below.

Save drafts and fully reopen the app, or start a new CLI process, to load a
changed provider configuration. No relay rebuild or restart is needed.
The desktop helper remains print/check-only; do not run the CLI writer against
desktop configuration as a migration shortcut.

## Acceptance and evidence

The initial manual passes preceded the explicit configuration change. The user
subsequently reported another successful `/compact` test after that change,
before the container refresh. The follow-up did not specify which client was
retested or independently establish a client reload. After the reviewed
container refresh, the user explicitly confirmed successful manual `/compact`
in both Codex CLI and the desktop app on October 26, 2026. This closes the
post-refresh manual-completion check. These reports do not
identify the underlying request path, qualify every client/model version,
or prove automatic compaction at the context limit. For fuller acceptance:

1. Use a non-sensitive conversation with known task facts and constraints.
2. Run `/compact`.
3. Ask for those facts without rereading files, then continue a permitted tool
   operation. Confirm the constraints and tool results remain usable.
4. If request-path verification is needed, inspect only sanitized route/status
   metadata. The workflow should use ordinary `/codex/v1/responses`, not
   `/codex/v1/responses/compact`. Do not enable body logging.

Record manual completion, retained context, tool continuation, and automatic
compaction separately. New inference tests require the normal explicit approval.

## Request-byte limits

Token context and serialized HTTP request size are separate limits. Reasoning
items, tool outputs, images, tool schemas and JSON overhead all occupy request
bytes. A conversation can exceed an upload cap before its token meter is full.
The relay's upload default is now 32 MiB, up from 16 MiB; explicit operator
overrides remain effective. An upstream service may have a different limit.

For Codex clients that repeatedly retry HTTP 413, opt in to terminal byte-size
errors by setting `COPILOT_CODEX_TERMINAL_PAYLOAD_ERRORS=1` in the existing ignored
relay environment file. Both local and upstream 413s become HTTP 400 with
`request_too_large`, not `context_length_exceeded`. Original status and origin
are retained in `X-Relay-*` headers and content-free logs. Claude is unaffected.
This stops retries but does not reduce history or make an oversized conversation
resumable by itself.

To try earlier automatic compaction, merge the relevant model into the existing
`COPILOT_CODEX_MODEL_OVERRIDES` JSON object, for example:

```json
{"gpt-6-astra":{"auto_compact_token_limit":300000},"gpt-6.2-astra":{"auto_compact_token_limit":300000}}
```

The 300000-token value is an experimental starting point, not a measured safe
byte limit or a new advertised context window. No earlier threshold is enabled
by default. The relay accepts positive integers up to 10000000, clamps the
advertised threshold to 90% of the model's validated context (minimum one), and
leaves other models unchanged. Existing Boolean capability overrides can remain
in the same object. Using relay-side per-model policy avoids a CLI helper
regeneration overwriting a manually added profile setting.

Alternatively, Codex's `model_auto_compact_token_limit` can be set in the
applicable client root/profile configuration, not the provider table. Client
configuration can override catalog policy; verify the effective model and
threshold rather than assuming a fresh catalog is sufficient. Do not run the
CLI writer merely to migrate the desktop setup.

Changes to relay environment settings require an approved recreation using a
validated image that contains this implementation, not just a container restart.
Preserve existing credentials and overrides. Fully reopen the client afterward
so it reloads model metadata. See [operations](operations.md#image-selection-before-recreation).
Repository edits alone change neither the live relay nor a running client.

Use the metadata-only [request-size diagnostics](operations.md#request-size-diagnostics)
to tune the threshold. Run `/compact` before the observed failure zone, not only
after a size rejection: local harness summarization still sends history through
the relay and can itself exceed the cap. A lower reasoning effort or smaller
tool outputs may reduce growth, but do not assume a fixed bytes-per-token ratio.
Do not silently remove reasoning items or tool-call/result pairs to make a
request fit. If compaction cannot run, use a concise handoff in a new conversation.

After explicit inference approval, qualify automatic triggering, retention of
task constraints, and tool continuation with a non-sensitive conversation.
Offline size/error tests do not establish those runtime outcomes or Copilot's
maximum accepted upload. Byte-aware compaction requires client support; the
relay does not autonomously summarize, truncate, or retry a user's history.

## Legacy API compatibility

The relay retains `/codex/v1/responses/compact` for callers that explicitly use
that API. It is not part of the supported CLI/app compaction workflow.
The earlier native endpoint probe returned HTTP 404 for the tested
`gpt-6-astra` route. That is an API compatibility limitation, not a blocker for
the user-tested `/compact` command.

Leave `COPILOT_ENABLE_SYNTHETIC_COMPACTION` disabled. Its ordinary-summary
fallback is not the harness's local compaction implementation and is not a
qualified encrypted checkpoint replacement. `scripts/qualify-checkpoint.py`
remains an optional API diagnostic, not a required CLI/app acceptance gate.

## Local configuration follow-up: October 26, 2026

The existing `copilot` and `copilot_desktop_pilot` provider tables were updated
with only the explicit remote-compaction capability. Byte-preservation checks
confirmed no other configuration changes. No client restart, inference request,
or relay recreation was performed.

Both installed client binaries contain the `remote_compaction` field, but an
isolated startup probe was blocked by the execution policy. Static field
presence is not proof that the setting controls runtime routing. Generated
configuration is covered by semantic TOML checks. The subsequent user report
confirms manual completion after the configuration edit; routing, retained
context, and tool continuation were not independently verified. After the
[container refresh](deployment-2026-10-26.md#reviewed-image-refresh), the user
confirmed manual completion in both clients. Automatic context-limit triggering,
retained task facts, and post-compaction tool continuity remain separate,
unverified acceptance items.
