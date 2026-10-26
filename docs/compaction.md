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
compaction. Do not invent a token threshold to suppress automatic compaction.

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
