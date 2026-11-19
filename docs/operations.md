# Production operations

Since version 0.6.0, immutable production deployment is separate from development.
Deployment and app restarts are separate, approval-gated actions. These files
do not change an already-running relay merely because they were edited.

## Deployment contract

- `docker-compose.yml` selects the production target and versioned image,
  listens only on `127.0.0.1:4141`, and has no source or test mounts.
- Runtime contains an installed wheel and runtime dependencies, not uv, test
  frameworks, or editable source. Base image indexes and the build backend are pinned.
- The container is non-root, read-only, capability-dropped, and disallows privilege
  escalation. Defaults: 1 GiB memory, two CPUs, 128 PIDs, 32 MiB scratch space.
- Uvicorn gets 20 seconds to drain; Docker allows 30 seconds before forced stop.
  Long requests can be interrupted by maintenance. Arrange a quiet period first.
- Logs rotate at 10 MiB, retaining three files. HTTP access logs are disabled to
  avoid recording arbitrary query strings. Application summaries retain request
  IDs, status, model, and timing without logging bodies by default.
- Docker's restart policy handles process exits, not upstream outages or an
  unhealthy status. It is not a substitute for diagnosis.
- `docker-compose.dev.yml` is an independent development stack on port 4142.
  Do not merge it into the production Compose file.

## Liveness and readiness

| Endpoint | Meaning |
|---|---|
| `/livez` | Local process responds, with package version; no upstream call |
| `/claude/readyz` | Nonempty selectable Claude catalog; 503 otherwise |
| `/codex/readyz` | Nonempty intersection of both Codex catalogs; 503 otherwise |
| `/codex/readyz?model=<exact-model-id>` | Additionally requires the selected model |
| Existing `/healthz`, `/claude/healthz`, `/codex/healthz` | Legacy diagnostic shape; HTTP 200 does not establish upstream success |

Readiness probes are bounded metadata requests, not inference. A ready catalog
does not prove tool support or a successful conversation. Liveness remains
available under relay admission saturation; readiness can return overload.

## Container and operator contract

The project provides a Linux container image and reference Compose files, not
a host installation or startup service. Choose your own Docker implementation,
host OS, credential provisioning, and lifecycle tooling. The server requires
`COPILOT_GITHUB_TOKEN` in its runtime environment and listens on container port
4141. It does not need host Python, PowerShell, a client CLI, desktop software,
or a mount of a host credential store.

The production image runs non-root with installed runtime dependencies and no
editable source. The reference Compose service additionally sets loopback-only
port publishing, a read-only filesystem, bounded writable scratch space,
resource/log limits, and local liveness checks. Preserve these safeguards in
another deployment rather than treating this unauthenticated local relay as a
public API. Container restart policy does not install or start the host engine.

In the reference setup, Compose reads `.env` both for interpolation (such as
`RELAY_IMAGE`) and, through the service's explicit `env_file`, for container
configuration. Other orchestrators should inject the corresponding runtime
settings securely; they do not have to use a file named `.env`. Never bake the
token into an image or publish a fully resolved secret-bearing configuration.

Optional PowerShell maintenance tools assume the Docker CLI sees their process
environment and that bind sources are valid for the selected engine. Docker
bind mounts use the daemon's filesystem, which may differ from the client's.
Custom launchers, remote engines, path translation, and environment forwarding
belong to the operator's setup, not the relay. The
[image-based checks](../README.md#development) need no host source mounts.

## Resource limits

All limits are per process. Keep a single worker unless accounting is redesigned.
Tune limits together with container memory and workload measurements.

| Environment setting | Default | Behavior |
|---|---:|---|
| `COPILOT_MAX_REQUEST_BYTES` | 33554432 | Upload cap, including chunked requests; 413 on overflow unless Codex compatibility below is enabled |
| `COPILOT_MAX_RESPONSE_BYTES` | 33554432 | Complete non-streaming and generated catalog cap; 502 on overflow |
| `COPILOT_MAX_CATALOG_BYTES` | 2097152 | Decoded upstream model catalog cap |
| `COPILOT_MAX_CATALOG_MODELS` | 512 | Maximum upstream rows before catalog expansion |
| `COPILOT_MAX_INFLIGHT` | 8 | Includes uploads and response lifetimes; 503 with Retry-After when full |
| `COPILOT_REQUEST_BODY_TIMEOUT` | 30 | Total upload deadline in seconds; 408 on expiry |
| `COPILOT_REQUEST_TIMEOUT` | 900 | Handler/response deadline after upload, in seconds |
| `COPILOT_CODEX_TERMINAL_PAYLOAD_ERRORS` | 0 | Opt-in Codex-only local/upstream 413 to terminal 400 mapping; byte-overflow code and original status retained |

The upload default is now 32 MiB instead of 16 MiB. Existing explicit
`COPILOT_MAX_REQUEST_BYTES` settings still take precedence. This is not a
promise that Copilot accepts a 32 MiB request. The relay buffers uploads and
parses JSON, and concurrent requests can consume substantially more than their
raw sizes. Measure memory at representative concurrency before raising the
cap further or treating the 1 GiB deployment limit as qualified for every
worst-case workload. Neither the concurrency default nor container memory has
been increased.

After upload, one disconnect watcher cancels abandoned work and releases its
admission slot after upstream cleanup. The overall deadline bounds trickling
responses, not just idle sockets. Before response headers it produces 504;
after headers it aborts the response without inventing successful completion.
Cleanup is cancellation-protected. HTTPX's existing connect/read/write timeouts
still apply. Catalog requests additionally have a total deadline.

Outbound requests negotiate `gzip, identity`, replacing client encoding preferences.
Gzip and zlib-wrapped deflate decode in bounded chunks. Unknown encoding chains,
invalid compressed data, and truncated compressed streams fail rather than
being forwarded as success. Error-body limits preserve known upstream auth and
rate-limit status even when diagnostic body reads fail or exceed the cap.
Transport-close failures are sanitized and must not replace a selected response;
EOF cleanup errors encountered while reading success bodies return structured
protocol errors. Caller cancellation remains distinct from cleanup failure.

## Compaction and model metadata

Use harness-managed `/compact` for Codex CLI and the ChatGPT Codex app.
Both setup helpers set `capabilities.remote_compaction = "unsupported"` on the
provider so compaction uses the local harness path rather than the standalone
API. This does not disable automatic context-limit compaction.
See [configuration, migration, and acceptance](compaction.md).

The legacy compact API route remains for explicit callers, not the supported
CLI/app workflow. Its native upstream probe returned 404 for the tested route.
Leave `COPILOT_ENABLE_SYNTHETIC_COMPACTION` disabled; its fallback is neither
harness-managed compaction nor a qualified encrypted checkpoint replacement.

Context windows and reasoning options come from validated upstream fields.
Missing or malformed context metadata uses 8192 tokens, not an assumed large
window. Missing reasoning options produce no advertised reasoning tiers.
Vision requires an explicit upstream `true`. Optional verbosity, parallel calls,
and search advertisement default off; requests are not silently rewritten to
remove manually supplied tools.

After qualifying a particular model, operators can explicitly enable only the
tested capabilities with non-secret JSON, for example:

```json
{"qualified-model":{"supports_search_tool":true,"supports_parallel_tool_calls":true}}
```

Set this as `COPILOT_CODEX_MODEL_OVERRIDES`; record evidence in the compatibility
matrix. It does not change routing, model IDs, permission settings, or provide a
fallback model. Requalify the generated catalog against the app's bundled engine.
The same per-model object also accepts `auto_compact_token_limit`, a positive
integer up to 10000000, clamped to 90% of the validated context window (minimum
one token). It is absent unless configured and does not change either context
window field. See [earlier compaction and payload recovery](compaction.md#request-byte-limits).

## Request-size diagnostics

Codex requests emit INFO-level `request_size` summaries even with `LOG_BODIES=0`.
They include exact incoming/outgoing byte counts, the configured upload cap,
an 80%-of-cap `near_limit` indicator, and compact-JSON size estimates for
message, reasoning, tool-call, tool-result, other input items, tools and
instructions. Images are included in their containing input item category.
Category sizes exclude original whitespace, array separators and other envelope
fields; they must not be summed as an exact wire-size measurement. Estimates
describe the rewritten request. A shared traversal budget of 8192 JSON encoder
chunks bounds estimation across nested input items, tools and instructions.
If exhausted, or if estimation fails, `estimates_available=0` and category sizes
are zero; exact incoming/outgoing lengths and the input count remain available.
This avoids an unbounded per-item diagnostics pass stalling the server loop.
The model label is only a fixed family (`gpt`, `claude`, or `other`), and reasoning
effort is allowlisted. No tool names, argument text, ciphertext, image data,
caller IDs, or arbitrary model identifiers appear in these summaries.

Local upload rejection logs are WARNING-level and include `origin=relay_upload`,
declared bytes (or -1 when unknown/unusable), bytes observed so far, and the cap.
An early Content-Length rejection observes zero body bytes. Upstream rejection
logs distinguish `upstream_payload` (HTTP 413), `upstream_context` (HTTP 400
with known `context_length_exceeded`), and other `upstream` errors. Relay-generated
request IDs correlate admitted Codex size logs, upstream errors and
`X-Relay-Request-Id` response headers. Correlation IDs are not supplied by callers.

Known, bounded upstream error codes and parameter names are retained in the
OpenAI error envelope. Unknown fields, malformed JSON and error bodies reaching
the diagnostic cap do not supply structured fields. Existing bounded reads and
redaction remain in effect. Auth/rate-limit status and forwarding headers are
preserved. Payload errors omit Retry-After because replaying unchanged bytes
cannot repair the request.

With `COPILOT_CODEX_TERMINAL_PAYLOAD_ERRORS=1`, both local and upstream Codex
HTTP 413 responses become HTTP 400 with `error.code=request_too_large`.
`X-Relay-Original-Status: 413` and `X-Relay-Error-Origin` retain the distinction.
Default behavior remains 413; Claude responses are unchanged. This compatibility
policy targets the inspected Codex CLI 0.170.0 behavior where generic 413 errors
are retryable but 400 invalid requests are terminal. Other client versions need
qualification. It stops futile retries, not the underlying overflow, and never
relabels byte overflow as `context_length_exceeded`. Native SSE error events
already inside HTTP 200 streams are still passed through, not rewritten.

## Upgrade and rollback

1. Record the running image ID, relay version, current readiness, and versions of
   any clients being qualified. Never dump the container environment or `.env` into logs.
2. Preserve the previous image under a unique rollback tag and record its exact
   ID before moving any default deployment tag. Do not overwrite the rollback
   tag or prune the image.
3. Build a new versioned image and run the [image-based checks](../README.md#development).
   Validate deployment configuration, image startup and shutdown with the
   optional maintenance helpers or equivalent automation for your environment.
4. Get approval for a maintenance window, preserve drafts, and wait for active
   requests to finish. Recreate using the validated image, not just a restart.
5. Check liveness, both required readiness routes, and approved synthetic
   inference. Confirm the running image is the validated artifact.
6. On failure, get approval to recreate with the recorded previous image.
   Use the durable image selection and, only when needed, the legacy override below.
   Do not restore old configuration over later user edits.

Keep configuration schema compatibility in the release checklist. Image rollback
alone does not undo credential rotation or subsequent app configuration changes.
The live deployment is not automatically upgraded by a successful build.

### Image selection before recreation

Compose's `RELAY_IMAGE` override or default `copilot-cli-relay:0.7.0` tag selects
the image for `up`; it need not identify the image currently running. A temporary
process override used during deployment does not persist after that process
environment is restored. A plain credential-renewal recreation can therefore
revert code if the selected tag still points to an older artifact.

Before any recreation, including token renewal or the helper's start/rebuild
actions, inspect image identities without printing resolved container environment:

```sh
# Read-only. Check each native command succeeds before using its output.
docker inspect copilot-cli-relay --format '{{.Image}}'
docker compose -f docker-compose.yml config --images
docker image inspect '<image-reference-from-config>' --format '{{.Id}}'
```

For token/settings-only recreation, the selected image ID must match the running
image ID. For an approved upgrade or rollback, it must match the recorded,
validated target ID instead. Stop on a missing image or unexpected mismatch;
do not pull, rebuild, or silently switch artifacts to make the check pass.
After an authorized deployment, persist the intended selection: either align the
default local tag after preserving rollback, or deliberately maintain a
`RELAY_IMAGE` override. Recheck the effective Compose selection because process
environment overrides can supersede `.env`.

Use `--no-build --pull never` for configuration-only recreation after those
checks. Changing tags does not change running containers. The dated
[deployment record](deployment-2026-10-26.md#default-image-tag-alignment) records
the approved local tag alignment; it is evidence, not a universal image ID.

### Legacy-image rollback compatibility

Images predating 0.6.0 do not expose `/livez`. The rollback override selects the
recorded image and restores the compatible `/claude/healthz` HTTP probe without
adding source mounts or loosening container restrictions. This legacy health
probe does not establish upstream readiness.

For a normal rollback to an image exposing `/livez`, update `RELAY_IMAGE` in the
ignored `.env` to the recorded, validated image and use the normal Compose file.
For an older image without `/livez`, also set `RELAY_ROLLBACK_IMAGE` in that file
to the same image reference, and use the legacy override:

```sh
# Inspect first; stop on failure or an unexpected image selection.
docker compose -f docker-compose.yml -f docker-compose.rollback.yml config --images

# Only after rollback approval and a quiet maintenance window.
docker compose -f docker-compose.yml -f docker-compose.rollback.yml up -d --no-build --pull never --force-recreate --timeout 30 proxy
```

The rollback selector has no default, preventing accidental selection of an
arbitrary image. Keep the intended image selection durable. While running a
legacy image, retain the health override on subsequent recreations; remove it
when upgrading back to an image that supports `/livez`. Preserve credentials
and unrelated settings. Verify the running image and appropriate health route
afterward; real inference is a separate approved check.

Maintainers with PowerShell 7 can optionally run
`pwsh scripts/test-rollback.ps1 -Image '<recorded-local-image-id>'` as an offline
adapter rehearsal. It uses synthetic configuration and does not perform a live
rollback or establish real client recovery.

## Diagnostics and credential renewal

For Windows desktop-client startup errors, the optional `codex-desktop.ps1 -Check`
helper compares installed and running packages before attributing the problem
to the relay. Unreadable process paths remain indeterminate. Its engine-version
probe has a deadline and bounded output; it never stops the app. This check-only
helper does not write configuration or repair shortcuts.

For upstream 401/403, deliberately refresh the token through your authorized
credential workflow (the Windows `extract-token.ps1` helper is optional),
verify the selected image as above, then recreate
the container after approval with `--no-build --pull never`.
Restart alone does not reload `.env`. Do not enable body logging or share raw
app logs merely to prove routing. Report sanitized error category, request ID,
versions, and the failing layer.

## Release gates

See [compatibility and qualification](compatibility.md). Automated CI is
read-only and does not deploy, publish, or run paid inference. Repository branch
protection and required-review settings are not enabled by adding a workflow;
configure those separately with explicit authorization.

`scripts/check-compose.ps1` copies only the two Compose files and `.env.example`
to a task-owned temporary directory and creates an empty synthetic `.env` there.
With `-RollbackImage <exact-image-id>`, it also copies and validates the rollback
override, temporarily setting and then restoring its process-scoped image
selector. `scripts/test-rollback.ps1` uses this same isolated validation path
before starting its synthetic test container.
Some Compose versions require `env_file` to exist even with
`--no-env-resolution`. The validator never copies or reads the real `.env`,
never starts containers, and removes only its own known scratch files.
Missing/unsafe Compose configuration and native command failures still fail
the check; the synthetic file is not a production fallback.

The deployment and six-request evidence from October 26, 2026 is recorded in
[the deployment report](deployment-2026-10-26.md). Active-stream shutdown and
legacy-image startup were rehearsed in network-isolated containers, not by
interrupting the working live deployment.
