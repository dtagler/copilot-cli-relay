# Codex desktop integration

The unified ChatGPT desktop app includes a **Codex** experience. This guide
configures its local model requests through the existing relay:

```text
ChatGPT app (Codex, This computer)
  -> bundled Codex engine
  -> http://127.0.0.1:4141/codex/v1
  -> GitHub Copilot /responses
```

No app patch, TLS interception, new OAuth flow, or separate proxy is required.
This configures local Codex model requests, not every app connection.
The Copilot integration remains unofficial and account/model availability can change.

## Product name and support boundary

**Codex desktop integration** is a first-class, project-supported feature for
local Codex chats in the Windows ChatGPT app. Project support means best-effort
maintenance, regression tests, and documented setup and troubleshooting for the
tested combinations below. It is not vendor support, an SLA, or certification of
every app version, model, or workflow. The [limits](#limits) remain in effect,
including unverified automatic context-limit compaction. Manual `/compact` was
user-confirmed in both clients on October 26, 2026; see [compaction](compaction.md).
The five-check GUI workflow below
has a dated user-confirmed pass, not blanket qualification of all recovery cases.

As checked on October 18, 2026, OpenAI's [desktop app documentation](https://learn.chatgpt.com/docs/app)
uses **ChatGPT desktop app** and distinguishes ChatGPT from Codex. In the app,
select **Codex**, then start a local chat using **Work in > This computer**.
Do not use ChatGPT Chat/Work or a cloud task to test this relay connection.

The helper and this guide retain their `codex-desktop` filenames. Existing
`copilot_desktop` and legacy provider IDs also remain valid. They identify the
Codex integration, not a requirement to install a separate product.

| Surface | Qualification |
|---|---|
| Windows ARM64 app, local Codex chat | Tested combination recorded below |
| App's bundled Codex engine with synthetic configuration | Tool and history-resume tests against the updated relay |
| Local Worktree mode | Runs on the computer, but not separately qualified here |
| Other OSes, package layouts, app versions, or models | Not established by the recorded Windows validation |
| ChatGPT Chat/Work and cloud Codex tasks | Outside this integration |

The [official environment guide](https://learn.chatgpt.com/docs/environments/modes)
distinguishes local and cloud execution. A cloud task's `127.0.0.1` is not this
computer's relay. The [glossary](../GLOSSARY.md) records the naming used here.

## Prerequisites and checks

Use the already-installed PowerShell 7, Docker relay, and Windows ChatGPT app.
Follow the README's [shared relay setup](../README.md#start-the-shared-relay)
if needed. App-only use does not require a separate Codex CLI install. Keep
Copilot credentials in the relay, not in the app's configuration. This setup
does not replace or modify your existing ChatGPT sign-in.

```powershell
# Read-only health, catalog, and installed-app checks. No inference requests.
pwsh scripts\codex-desktop.ps1 -Check -Model gpt-6-astra

# Print a configuration fragment. Does not write any files or environment values.
pwsh scripts\codex-desktop.ps1 -Model gpt-6-astra
```

Choose a model returned by the relay's Responses catalog. The example model is
not a promise of access on every Copilot account. Checks fail if the selected
model is absent; the helper does not silently substitute a model.

`-BaseUrl` supports the same namespace on a different loopback port.
`-ProviderId` lets you use a different non-reserved provider name. For example,
use `-ProviderId copilot_desktop_pilot` to retain that ID in an existing configuration.

The check requires `upstream_ok`, an upstream HTTP 200, and a positive Responses
model count. It also requires both model-list arrays. The health endpoint's
HTTP 200 or `ok: true` alone is not evidence of a healthy Copilot connection.

The installed-app check currently looks for `OpenAI.Codex` and its
`app\resources\codex.exe`. These are the tested Windows package/executable
identifiers, even though the UI is branded ChatGPT. A future packaging change
can make `-Check` fail; it does not trigger a fallback install or app modification.

**A passing check is not an app inference test.** It does not read private
Codex configuration, establish the winning provider/profile, load the catalog
through the app, or exercise tools.

## Merge the configuration deliberately

Codex in the app reads the shared user-level `config.toml`, normally
`%USERPROFILE%\.codex\config.toml`. Account for `CODEX_HOME` and managed settings
if your installation uses them. Trusted project configuration and a selected
profile can change effective values; see the
[configuration precedence reference](https://learn.chatgpt.com/docs/config-file/config-basic).

1. Merge the generated **root** `model` and `model_provider` settings before the
   first TOML table. Replace an existing value rather than adding a duplicate.
2. Add the generated provider/auth tables, or carefully merge an existing
   provider with that ID. Keep your unrelated settings and CLI provider intact.
3. The default `copilot_desktop` ID is separate from the CLI's `copilot` provider.
   Leave a customized `copilot.config.toml` alone. A CLI `--profile copilot`
   selection does not itself select that profile in the app.
4. Check for an effective `model_catalog_json`, including managed/profile
   settings. It takes precedence over `model_catalog_url`; do not remove an
   existing catalog without deciding how to preserve its intended behavior.
5. Save your work and preserve any unsent drafts, then fully restart the
   ChatGPT app. Open a fresh local Codex chat for verification.

Changing this shared root provider also changes CLI launches without an explicit
profile. Provider IDs can be referenced by saved threads, so do not remove a
legacy provider merely to rename it. Keep an explicit record of your edits for
targeted rollback; never overwrite later app/user changes with a whole-file restore.

The generated command auth uses **one** resolved PowerShell executable and emits
only the non-secret `dummy` token. It does not depend on terminal-only environment
variables or put a Copilot credential in the app. Do not combine it with a
different provider-auth mechanism.

The helper deliberately has **no write, migration, app-launch, or installation
mode**. In particular, it does not reuse `codex-config.ps1 -Write`, which manages
the CLI profile and legacy environment setup. It also does not change sandbox,
approval, permission-profile, or browser-guard settings.

### Configure harness-managed compaction

The generated provider sets `capabilities.remote_compaction = "unsupported"`.
Use `/compact` in the conversation rather than the standalone compact API.
For existing providers, merge the setting without replacing names, IDs, auth,
or other capabilities. See [compaction migration and acceptance](compaction.md).
This selects local compaction; it does not turn off automatic compaction.

### Update an existing provider's display name

The generated provider display name is **GitHub Copilot for Codex in ChatGPT**.
If the app settings still show **Copilot Desktop pilot**, change only the `name`
value in the existing provider table in your user `config.toml`:

```toml
[model_providers.copilot_desktop_pilot]
name = "GitHub Copilot for Codex in ChatGPT"
# Keep all existing provider settings and the matching auth table unchanged.
```

This is an illustration of the existing table, not an additional table to append
or a replacement for the full provider configuration. Keep the table ID, root
`model_provider`, model, URLs, credentials, and permission settings unchanged.
Saved conversations may reference that ID. Do not rename profile files or edit
conversation storage to change a display label.

Save drafts before fully restarting the app if it still shows the old name.
The display-name change does not require a relay restart.

## Qualify the actual app workflow

Use a fresh local Codex chat with a small, non-sensitive fixture folder. Select the
least privilege appropriate to your test. Model routing does not authorize local
tools, MCP connections, or app access. Full access is not a relay prerequisite.

Verify these independently:

1. Confirm the intended provider/model, then request a short streamed answer,
   such as `Reply with exactly: relay-restart-ok`.
2. Ask for a local file read. Check the tool call, matching result, and final answer.
3. Ask a context-dependent follow-up without rereading the file.
4. Stop a longer answer, then verify that another request succeeds.
5. Leave and reopen the test thread, then verify a context-dependent response.
6. Correlate those actions with the relay, rather than asking the model to identify
   itself. Do not enable body logging just to establish routing.

Correlate a controlled fixture run with relay summary timestamps and request IDs.
Production HTTP access logs are disabled, so query-parameter markers are not an
attribution mechanism. Do not enable raw body logging; another client can use the
same provider, so keep unrelated requests out of a qualification window.

### Recorded validation

Observed on October 18, 2026. The initial GUI test, updated-relay engine tests,
and post-restart user confirmation are distinct evidence:

| Surface | Evidence |
|---|---|
| Windows ARM64 package `OpenAI.Codex` `26.1013.4894.0`, bundled engine `0.163.0-alpha.10.2` | Initial native relay catalog discovery and GUI file-read/tool-result/final-answer loop with `gpt-6-astra`, before the later relay restart |
| GUI permission mode | User selected Full access; this was not a read-only GUI isolation test or a required relay setting |
| Bundled engine, separate synthetic configuration | Text response, read-only MXC file/tool round trip, and process-resumed history; repeated against the updated relay in a disposable container using the generated configuration |
| Live relay after restart | Both upstream health checks passed; 19 Responses models were available; container source matched the validated streaming implementation |
| Post-restart app text check | User reported the exact `relay-restart-ok` response. This is a user-confirmed text smoke test, not a new independently observed GUI tool/recovery test |
| Automated relay tests | Fragmented SSE, function/custom tool replay, post-header disconnect, repeated cancellation during cleanup, one-shot transport closure, early close, and downstream send failure |
| Further GUI qualification as of October 18 | Follow-up, cancellation/recovery, and reopening were unverified at that time; no existing draft was overwritten to automate them |

On October 26, 2026, the user confirmed all five manual checks passed: the
renamed provider label, actual fixture read, follow-up without rereading,
streamed stop/recovery, and reopening with retained fixture values. These are
user-reported results, not agent-observed GUI tests. See the
[dated acceptance record](remaining-qualification.md#user-assisted-gui-acceptance)
for version and evidence boundaries.

This is a bounded compatibility result, not blanket support for all app builds,
Copilot models, permission modes, or Desktop features. Match model metadata to
the bundled engine and upstream capabilities when testing another combination.

## Limits

- Use HTTP/SSE with full replay. The helper disables WebSockets. The relay strips
  unsupported `previous_response_id` rather than implementing incremental state.
- Unsupported image-generation tools are removed. Image-input passthrough is
  not a guarantee of image inference support on every model.
- Manual `/compact` is user-confirmed in both clients. Generated configurations
  select harness-managed compaction; automatic triggering and post-change
  retained-context/tool continuation require separate verification.
- The legacy `/responses/compact` fallback is disabled unless explicitly opted in with
  `COPILOT_ENABLE_SYNTHETIC_COMPACTION=1`. It wraps an ordinary summary and has
  not been qualified as a safe replacement checkpoint for long Desktop sessions
  or as v2 compaction. Long-session checkpoint compaction remains outside the
  qualified support scope.
  The fallback now rejects failed, incomplete, empty, or malformed summaries
  with an OpenAI-shaped 502; that validation does not establish checkpoint safety.
- Heartbeats are emitted only between complete SSE events. A pause inside a
  partial event, or at an ambiguous CRLF split, intentionally receives no ping.
- The relay now watches downstream disconnect after upload, including while
  waiting for upstream headers, and bounds total request time. Synthetic tests
  cover cancellation and cleanup; the October 26 user-confirmed GUI streamed
  stop/recovery result does not independently measure pre-header cancellation
  or upstream cleanup timing.
- An upstream failure in the middle of an already-forwarded JSON event cannot
  be repaired by appending an error frame. The relay never fabricates successful
  completion for a truncated stream.
- ChatGPT Chat/Work, cloud orchestration, voice, plugins, MCP, and other app services can use
  separate endpoints and credentials. Do not weaken authentication or permission
  controls to make an unrelated UI feature appear.
- The dummy token is only a client compatibility mechanism, not per-user
  authentication. Keep the relay loopback-only. Local tools execute in the Codex
  environment; prompts and tool results submitted for inference reach Copilot.

## Troubleshooting

| Symptom | Check |
|---|---|
| `-Check` passes, but the app does not use the relay | Confirm Codex and This computer are selected. Inspect the winning root model/provider, project/profile settings, and any `model_catalog_json` override, then restart the app after saving drafts. |
| Expected model is absent | Use the same model in `-Check -Model ...` and the generated configuration. It must appear in both catalog arrays. Do not silently substitute a model or assume a bundled picker proves relay discovery. |
| Auth helper fails | Regenerate the print-only snippet and compare its single absolute PowerShell path with the selected provider's auth table. Keep the dummy auth separate from the real Copilot credential. |
| Upstream Copilot returns 401 | Refresh the relay token deliberately, verify [selected/running image IDs](operations.md#image-selection-before-recreation), then use `docker compose up -d --no-build --pull never --force-recreate proxy`. A container restart alone does not reload `.env`. |
| Windows package lookup fails | The helper expects the tested `OpenAI.Codex` layout. Check installed app packaging; do not rename provider IDs, install a second app, or modify packaged files as a workaround. |
| A long conversation stalls or cannot resume | Separate streaming, history replay, and compaction. See the limits above; a text smoke test does not qualify recovery or long-session compaction. |
| Tools are denied even though text works | Review the app's execution permissions and applicable policy independently. The relay does not grant tool access or require Full access. |

Do not run `codex-config.ps1 -Write` as a generic app repair: it manages the
CLI's provider/profile and legacy environment setup. Use the print-only app
helper and preserve unrelated config, saved provider IDs, and existing drafts.

For rollback, undo only the root settings and provider additions you recorded.
Do not restore an old whole-file snapshot over later app or user changes.

## Known issue: duplicate desktop output

On October 26, 2026, the user reported complete assistant replies displayed twice
in new and existing desktop chats, including after a full app restart. The
reported CLI workflow did not duplicate replies. The observed desktop package
was 26.1021.3705.0 with bundled engine 0.164.0-alpha.11.1; the CLI was 0.164.0.
These are different client builds, not a version-matched comparison.

Inspected desktop turns contained one persisted assistant answer and one matching
completion event with the same message ID. The screenshot showed two full text
blocks under one set of reply controls. This narrows the investigation, but does
not establish the identity/count of every live streaming event or prove that the
relay and upstream are uninvolved.

[Codex issue #42643](https://github.com/openai/codex/issues/42643), consulted
October 26, 2026, describes similar desktop duplication with custom
Responses-compatible providers. Its follow-up reports include final-answer
duplication and Windows observations. Similar symptoms are not proof of the
same cause or a confirmed fix.

The working classification is a presumed desktop-side compatibility issue,
accepted for continued relay qualification. No response-text deduplication,
event suppression, client permission changes, or relay workaround is enabled.
If investigated further, use a synthetic conversation and allowlisted event
metadata with consistently pseudonymized IDs; do not enable body logging.
Keep this issue separate from successful manual `/compact` completion.

## Development verification

Run Python checks in the existing Docker environment, as described in the README.
The Windows helper has dependency-free PowerShell tests:

```powershell
pwsh tests\powershell\test-codex-desktop.ps1
```

These tests use synthetic data and mocked fetches. They do not read user
configuration, invoke real model requests, or require Pester. Additional cases use
temporary loopback HTTP listeners and bounded synthetic child processes.

Use `scripts/test.ps1` for the current Python/coverage and PowerShell assertion
totals. Synthetic checks are separate from the dated live evidence above.

## Official references

Consulted on October 18, 2026. They document OpenAI's client configuration and
gateway contracts, not certification or support for this Copilot relay.

- [ChatGPT desktop app](https://learn.chatgpt.com/docs/app)
- [App developer settings and shared Codex configuration](https://learn.chatgpt.com/docs/developer-settings)
- [Codex local, Worktree, and cloud environments](https://learn.chatgpt.com/docs/environments/modes)
- [Configuration precedence](https://learn.chatgpt.com/docs/config-file/config-basic)
- [Codex gateway configuration](https://learn.chatgpt.com/docs/enterprise/connect-to-a-gateway)
- [Gateway compatibility](https://learn.chatgpt.com/docs/enterprise/gateway-compatibility)
- [Version-matched model catalogs](https://learn.chatgpt.com/docs/enterprise/roll-out-a-gateway)
