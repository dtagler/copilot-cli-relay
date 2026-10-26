# Deployment and qualification: October 26, 2026

The user approved recreating the relay with the validated 0.6.0 candidate and
running six potentially billable `gpt-6-astra` qualification requests.
The initial deployment ran that candidate. The later reviewed-image refresh is
recorded below. No app configuration or permissions were changed by either
deployment, and no release or commit was published.

## Artifact and deployment

- Deployed image:
  `sha256:40fba3f19c1de7fc7cafe68b92881c88bcd272cff2cc97282f397fb93ed393bd`.
- Preserved previous image:
  `sha256:c88fd128e16425bf4841cccaeeaf362456665b93d2c4034dec4b7a2b496f560c`.
- All 14 installed runtime module hashes matched source at initial deployment.
- Native Linux ARM64 artifact; Python platform `linux-aarch64`.
- Only host port `127.0.0.1:4141` is published. No source or test mounts.
- Read-only root; UID/GID 1000; all capability sets empty; no-new-privileges enabled.
- Limits: 1 GiB memory, two CPUs, 128 PIDs; 32 MiB restricted temporary filesystem.
- Log rotation: three 10 MiB files. Stop grace: 30 seconds; restart: unless-stopped.
- Live configuration confirms body logging and synthetic compaction are disabled.
- Liveness reports 0.6.0. Codex readiness for `gpt-6-astra` passes with 15
  selectable models; Claude readiness passes with five.

The candidate was built with pinned base images and locked dependencies using the
existing approved uv cache. A fresh dependency download remains TLS-blocked.
No certificate verification was disabled and no host security settings changed.

## Live six-request result

Started at 15:26:53 UTC. Harness exit code 0, elapsed 11.5 seconds.
The harness used the exact reviewed development image, shared only the relay's
network namespace, mounted no credentials or user files, and executed no tools
suggested by the model.

| Check | Result |
|---|---|
| Streamed text with successful terminal completion | Passed |
| Allowlisted synthetic tool call | Passed |
| Matching fixed tool-result replay and final answer | Passed |
| Context-dependent follow-up | Passed |
| Client stream closure followed by a successful new request | Passed |

Client-close/recovery is not proof of GUI stop behavior. These were programmatic
relay requests, not UI automation. The original six-request run did not include
compaction; the later native probe is recorded under Follow-up. Synthetic
compaction remains outside the qualified support scope.

## Desktop prerequisites

- App package: `OpenAI.Codex` 26.1021.3705.0, ARM64.
- Bundled engine: `codex-cli 0.164.0-alpha.11.1`.
- Running app paths match the installed package.
- Selected model: `gpt-6-astra`.
- Provider ID remains `copilot_desktop_pilot`, preserving saved references.
- Configured display name: `GitHub Copilot for Codex in ChatGPT`.
- Transport remains HTTP/SSE, with WebSockets disabled.

The settings popup was not automated or visually verified by the agent. The
available Windows automation excludes ChatGPT UI automation. Later that day,
the user confirmed all five manual GUI checks passed, including the provider
label, actual fixture read, follow-up, streamed stop/recovery, and reopening.
See the [user-confirmed record](remaining-qualification.md#user-assisted-gui-acceptance).
Checkpoint semantics are not established by those short-history checks.

## Isolated operational rehearsals

An additional production container with a synthetic loopback upstream and no
external network access was stopped during an active SSE response. A separate
observer received the terminal completion, and the server exited 0. This tests
draining a finite active stream, not every long-running upstream failure mode.

The preserved 0.5.0 image was started separately with no network access or real
credentials, under non-root/read-only restrictions. Its legacy health endpoint
worked. The rollback override preserves that endpoint because the old image
does not support `/livez`. No live rollback was necessary or performed.

Only the test-owned containers were removed; the live relay and prior image
remain intact.

## Evidence boundaries

- Independent deployment review verified the running artifact, source hashes,
  container restrictions and live metadata readiness.
- The six-request result is the implementing agent's observed harness result;
  the independent reviewer did not repeat potentially billable requests.
- GUI recovery/reopening results are user-confirmed, not independently observed.
  Hosted CI, clean remote dependency restoration, Windows x64 and other platforms,
  and long-session compaction are not newly qualified.
- The repository remains uncommitted; deployment is not publication or vendor support.

## Follow-up

The later [remaining-qualification investigation](remaining-qualification.md)
verified version-matched catalog consumption, recorded a native checkpoint HTTP
404, and reproduced the clean-build TLS failure. It also provides the synthetic
fixture and exact manual GUI checks. Those later results do not change what the
original six-request run established.

## Reviewed-image refresh

On October 26, 2026, the user reported successful manual `/compact` after the
client configuration edit, then authorized refreshing the Docker container.
Recreation and health checks completed by 17:59:53 UTC (12:59:53 America/Chicago).

- Running image:
  `sha256:a188da1fc930c714b37460093f3d49ef26de2461338fef5f97d7394c149ecf5c`
  (`copilot-cli-relay:review-20261026`).
- Previous image retained locally, then still tagged `copilot-cli-relay:0.6.0`:
  `sha256:40fba3f19c1de7fc7cafe68b92881c88bcd272cff2cc97282f397fb93ed393bd`.
- All 14 installed runtime module hashes were rechecked against current source.
  Compose safety, isolated image smoke/idle shutdown, and synchronized
  active-stream shutdown checks passed again before deployment.
- Recreated with the exact image ID using a temporary process-scoped
  `RELAY_IMAGE`, `--no-build`, `--pull never`, and a 30-second stop timeout.
  The prior process variable was restored. No images were rebuilt or pulled.
- Docker health is healthy; liveness reports 0.6.0. Codex readiness for
  `gpt-6-astra` passes with 15 selectable models; Claude readiness passes with five.
- Verified non-root user, read-only root, no bind mounts, loopback-only port,
  capability drop, no-new-privileges, resource limits, restricted tmpfs,
  log rotation, and restart/stop policies.
- Body logging and synthetic compaction remain disabled.

At refresh time, the Compose default tag still identified the previous image.
The subsequent tag alignment below resolves that mismatch. No `.env`, client
configuration, credentials, host installation, or Git state was changed by
the refresh.

No inference was sent during this refresh. Earlier six-request results belong
to the previous artifact. The user subsequently confirmed successful manual
`/compact` in both CLI and desktop against the refreshed container. That report
does not independently prove routing, automatic triggering, retained task facts,
or post-compaction tool use.

### Default image tag alignment

During pre-commit cleanup on October 26, 2026, the user authorized resolving the
default-image mismatch while preserving rollback:

- Verified the running image was the reviewed `sha256:a188da1f...` artifact above.
- Tagged the previous `sha256:40fba3f1...` artifact as
  `copilot-cli-relay:rollback-20261026-40fba3f19c1d`, and verified it before
  changing the default tag.
- Pointed `copilot-cli-relay:0.6.0` to the exact reviewed image already running.
- Verified effective Compose selection is `copilot-cli-relay:0.6.0` and that
  this tag resolves to the running image ID.

These were local image-tag changes only. No image was rebuilt, pulled, deleted,
or published. No container was recreated or restarted, and no credential or
configuration file changed. Before a future recreation, verify the selected image
again using the [operations checklist](operations.md#image-selection-before-recreation);
tags and process-level overrides can change independently of the container.
