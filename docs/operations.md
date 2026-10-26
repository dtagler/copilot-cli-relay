# Production operations

Version 0.6.0 separates immutable production deployment from development.
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

## Resource limits

All limits are per process. Keep a single worker unless accounting is redesigned.
Tune limits together with container memory and workload measurements.

| Environment setting | Default | Behavior |
|---|---:|---|
| `COPILOT_MAX_REQUEST_BYTES` | 16777216 | Upload cap, including chunked requests; 413 on overflow |
| `COPILOT_MAX_RESPONSE_BYTES` | 33554432 | Complete non-streaming and generated catalog cap; 502 on overflow |
| `COPILOT_MAX_CATALOG_BYTES` | 2097152 | Decoded upstream model catalog cap |
| `COPILOT_MAX_CATALOG_MODELS` | 512 | Maximum upstream rows before catalog expansion |
| `COPILOT_MAX_INFLIGHT` | 8 | Includes uploads and response lifetimes; 503 with Retry-After when full |
| `COPILOT_REQUEST_BODY_TIMEOUT` | 30 | Total upload deadline in seconds; 408 on expiry |
| `COPILOT_REQUEST_TIMEOUT` | 900 | Handler/response deadline after upload, in seconds |

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

## Upgrade and rollback

1. Record the running image ID, relay version, app/bundled-engine versions, and
   current readiness. Never dump the container environment or `.env` into logs.
2. Preserve the previous image under a unique rollback tag and record its exact
   ID before moving any default deployment tag. Do not overwrite the rollback
   tag or prune the image.
3. Build a new versioned image and run `scripts/test.ps1`,
   `scripts/check-compose.ps1`, and `scripts/test-image.ps1`.
4. Get approval for a maintenance window, preserve drafts, and wait for active
   requests to finish. Recreate using the validated image, not just a restart.
5. Check liveness, both required readiness routes, and approved synthetic
   inference. Confirm the running image is the validated artifact.
6. On failure, get approval to recreate with the recorded previous image ID via
   process-scoped `RELAY_ROLLBACK_IMAGE` and the rollback override below.
   Do not restore old configuration over later user edits.

Keep configuration schema compatibility in the release checklist. Image rollback
alone does not undo credential rotation or subsequent app configuration changes.
The live deployment is not automatically upgraded by a successful build.

### Image selection before recreation

Compose's `RELAY_IMAGE` override or default `copilot-cli-relay:0.6.0` tag selects
the image for `up`; it need not identify the image currently running. A temporary
process override used during deployment does not persist after that process
environment is restored. A plain credential-renewal recreation can therefore
revert code if the selected tag still points to an older artifact.

Before any recreation, including token renewal or the helper's start/rebuild
actions, inspect image identities without printing resolved container environment:

```powershell
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

```powershell
# Offline rehearsal only; the image must already exist locally.
pwsh scripts/test-rollback.ps1 -Image '<recorded-local-image-id>'

# Actual rollback requires separate approval and a quiet maintenance window.
$env:RELAY_ROLLBACK_IMAGE = '<recorded-local-image-id>'
docker compose -f docker-compose.yml -f docker-compose.rollback.yml up -d --no-build --pull never --force-recreate --timeout 30 proxy
Remove-Item Env:RELAY_ROLLBACK_IMAGE
```

If that process variable already had a value, preserve and restore it rather than
removing it. The override has no default image, preventing accidental selection
of an arbitrary prior version. Confirm actual inference after an approved live
rollback; the offline rehearsal intentionally uses no credentials or provider.

## Diagnostics and credential renewal

Use the helper's installed/running-package comparison before treating a desktop
startup error as a relay failure. Unreadable process paths remain indeterminate.
The engine version probe has a deadline and bounded output; it never stops the
desktop app. No helper writes configuration or repairs shortcuts.

For upstream 401/403, deliberately refresh the token with the existing
`extract-token.ps1` workflow, verify the selected image as above, then recreate
the container after approval with `--no-build --pull never`.
Restart alone does not reload `.env`. Do not enable body logging or share raw
app logs merely to prove routing. Report sanitized error category, request ID,
versions, and the failing layer.

## Release gates

See [compatibility and qualification](compatibility.md). Automated CI is
read-only and does not deploy, publish, or run paid inference. Repository branch
protection and required-review settings are not enabled by adding a workflow;
configure those separately with explicit authorization.

The deployment and six-request evidence from October 26, 2026 is recorded in
[the deployment report](deployment-2026-10-26.md). Active-stream shutdown and
legacy-image startup were rehearsed in network-isolated containers, not by
interrupting the working live deployment.
