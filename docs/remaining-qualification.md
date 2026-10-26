# Remaining qualification: October 26, 2026

The working 0.6.0 relay was not restarted or reconfigured during these checks.
The initial qualification did not change user configuration. The subsequent
[compaction configuration follow-up](compaction.md) selects harness-managed
compaction in the client providers. Credentials, app permissions, registry
settings, and TLS verification settings remain unchanged.
The later [approved container refresh](deployment-2026-10-26.md#reviewed-image-refresh)
is recorded separately. The user subsequently confirmed manual `/compact`
completion in both clients against the refreshed container.

## Completed: version-matched desktop catalog consumption

The installed Windows ARM64 bundled engine, `codex-cli 0.164.0-alpha.11.1`,
successfully consumed relay-shaped metadata for all 15 advertised models,
including `gpt-6-astra`.

`scripts/check-desktop-catalog.ps1` obtains the live relay catalog, adds a unique
display-name marker to a temporary copy, and serves it on an ephemeral loopback
port. The bundled engine runs `debug models` with a separate temporary
`CODEX_HOME` and a child environment that does not inherit API keys or proxy
credentials. Its output must contain that marker for the selected model; falling
back to the bundled catalog cannot pass. The server accepts the engine's
client-version query parameters but serves only the model-catalog GET.

This verifies actual bundled-engine configuration/catalog parsing without
inference or GUI automation. Only the child engine and task-owned files/server
are cleaned up. The real provider ID and display name remain unchanged.
The catalog download uses a total deadline and checks the raw byte budget before
JSON parsing. Redirects and compressed diagnostic responses are rejected.

```powershell
pwsh scripts/check-desktop-catalog.ps1 -Model gpt-6-astra
```

## Completed: user-reported manual compaction

On October 26, 2026, the user separately reported successful `/compact` in
Codex CLI and the ChatGPT Codex app. These reports precede the explicit
`capabilities.remote_compaction = "unsupported"` configuration change.
They establish manual command completion, not the underlying route,
automatic triggering, or independently observed post-compaction tool continuity.
The user subsequently reported another successful manual `/compact` after the
configuration edit and before the container refresh. That report did not specify
the client or establish routing; it is not a post-refresh acceptance result.

After the refresh, the user explicitly confirmed that manual `/compact` worked
in both Codex CLI and the desktop app. This later report closes that manual
smoke-test item, but does not establish request routing, automatic context-limit
triggering, retained task facts, or post-compaction tool continuity.
The subsequently reported [duplicate desktop output](codex-desktop.md#known-issue-duplicate-desktop-output)
is tracked separately as a presumed client-side compatibility issue, not a
confirmed relay defect or a failed manual-compaction command.

Harness-managed `/compact` is the supported workflow. See the
[configuration and post-change acceptance steps](compaction.md).
The standalone endpoint limitation below is not a CLI/app production blocker.

## Legacy API limitation: native checkpoint availability

The approved bounded probe sent one synthetic native compaction request through
the live relay for `gpt-6-astra`. It returned HTTP 404, so no continuation request
was attempted. This establishes unavailability for the tested route/configuration,
not a claim about every model, account, or future provider version.

`scripts/qualify-checkpoint.py` is plan-only unless `--run-inference` is supplied.
It sends at most two potentially billable requests: compact the synthetic
transcript, then resume only if a nonempty encrypted compaction item is returned.
The continuation checks task facts, a tool-result marker, and a no-write
constraint. It executes no model-generated tools and prints no response bodies.
An ordinary assistant summary cannot count as a native checkpoint.

The [official compaction guide](https://developers.openai.com/api/docs/guides/compaction),
consulted October 26, 2026, describes passing the complete returned compacted
window, including its opaque compaction item, into the next Responses request.
The new probe follows that contract. A passing short fixture would still not
qualify every long-session or desktop recovery scenario.

This optional API diagnostic is not a prerequisite for harness-managed `/compact`.
For a deliberate retest after provider support changes, mount the current harness
and runtime source into the existing development image; pin the image ID and
validate current source/dependencies first. No credentials or user files are mounted:

```powershell
# Explicit approval for inference and the exact model is required.
$image = docker image inspect copilot-cli-relay:dev-hardening --format '{{.Id}}'
if ($LASTEXITCODE -ne 0 -or $image -notmatch '\Asha256:[0-9a-f]{64}\z') { throw 'A validated local development image is required.' }
pwsh scripts/test.ps1 -Image $image
if ($LASTEXITCODE -ne 0) { throw 'Current source/dependency checks failed.' }
docker run --rm --pull never --network container:copilot-cli-relay --read-only --tmpfs /tmp:rw,nosuid,nodev,size=32m --mount "type=bind,source=${PWD}/src,target=/app/src,readonly" --mount "type=bind,source=${PWD}/scripts/qualify-checkpoint.py,target=/app/scripts/qualify-checkpoint.py,readonly" $image /app/.venv/bin/python scripts/qualify-checkpoint.py --model gpt-6-astra --run-inference
```

Exit 0 means the short native checkpoint/continuation fixture passed; exit 2
means native compaction was unavailable; exit 1 means another check failed.
Synthetic compaction remains disabled in production. Do not enable it merely to
remove this API limitation. Server-side automatic compaction and alternative
provider routes were not tested or enabled.

## Blocked: clean dependency download

A normal `docker build --no-cache --target production` was retried without the
seeded dependency cache. It again failed during locked wheel downloads from
`files.pythonhosted.org`, with a TLS `HandshakeFailure`.

Read-only controls narrowed the issue:

| Check | Result |
|---|---|
| PyPI simple index for Starlette | HTTP 200 |
| File-host HTTPS using Windows PowerShell/.NET | TLS handshake failure |
| File-host HTTPS using Windows curl/Schannel | TLS handshake failure |
| Windows curl constrained to verified TLS 1.2 | Same handshake failure |
| Locked Docker/uv restore using the pinned Python ARM64 image | Same file-host handshake failure |

This is not evidence of a broken dependency lock or an image-only configuration
issue. It does not identify whether the failing component is the network path,
filtering, or the remote service. No certificate check was bypassed, insecure
download enabled, mirror substituted, or host setting changed.

The remaining action is to restore authorized HTTPS connectivity to
`files.pythonhosted.org`, or supply an organization-approved package mirror for
an explicitly reviewed dependency-source change. Do not use `--insecure`,
trusted-host overrides, disabled verification, or an arbitrary mirror.

The current cache-built production image remains operational. Its successful
build and tests do not substitute for a clean-download result.

## User-assisted GUI acceptance

On October 26, 2026, the user confirmed completing all five checks below and
reported that they passed:

| Check | Result |
|---|---|
| Provider label shows GitHub Copilot for Codex in ChatGPT | User-confirmed pass |
| Actual fixture file-read tool/result returns the marker and total | User-confirmed pass |
| Context-dependent follow-up without rereading the file | User-confirmed pass |
| Stop a streamed response, then complete a new request | User-confirmed pass |
| Leave and reopen the same chat, then recall the fixture values | User-confirmed pass |

This closes the five-item manual GUI checklist for the reported local workflow.
These are user-reported results, not agent-observed or independently instrumented
GUI tests. The app/engine/model tuple was recorded earlier that day; it was not
re-inventoried at the time of this confirmation. The report does not establish
pre-header cancellation timing, upstream cleanup timing, checkpoint compaction,
or compatibility with other versions/platforms. The separate manual `/compact`
reports above do not change the legacy API HTTP 404 evidence or the clean-download
TLS blocker.

The available Windows automation explicitly excludes operating the ChatGPT UI.
For future retests, use a fresh **local Codex chat in this repository**, with existing security and
approval settings. Authorize only the fixture read if prompted; no write,
installation, or permission changes are needed.

1. Confirm the provider display name is **GitHub Copilot for Codex in ChatGPT**.
2. Ask: `Read tests/fixtures/desktop-acceptance.txt without modifying anything.
   Tell me the marker and total.` Inspect the actual file-read tool/result.
   Expected: `desktop-fixture-739` and `42`.
3. Ask: `Without reading files again, what are project_code and next_step?`
   Expected: `cedar` and `report_marker`.
4. Request a long numbered list, press Stop while output is streaming, then ask
   for the fixture marker again. Confirm that a new response succeeds.
5. Leave the chat, reopen the same chat, and ask for the marker and total again.
   Expected: `desktop-fixture-739` and `42`.

Report each result separately: provider label, tool read, follow-up, stop/recovery,
and reopening. Do not mark checkpoint qualification passed from these short
history tests. Save drafts before any optional full app restart.
For manual `/compact` and post-compaction continuation, use the separate
[compaction acceptance steps](compaction.md#acceptance-and-evidence).
