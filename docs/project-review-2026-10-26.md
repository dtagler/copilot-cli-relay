# Full-project review: October 26, 2026

This review covers the complete uncommitted 0.6.0 promotion/hardening work,
including new files, runtime interactions, Windows helpers, qualification
harnesses, packaging/CI, current documentation, and the bundled model-switch
skill. Earlier dated evidence and compatibility identifiers are preserved.

## Findings and remediation

### Bounded decoder negotiation

The new decoder did not support every encoding previously accepted by HTTPX,
but outbound requests still advertised deflate and Claude could inherit arbitrary
client encoding preferences. Synthetic raw-deflate and stacked-encoding fixtures
reproduced failures.

Both protocol header builders now explicitly negotiate `gzip, identity`. Claude
removes every case variant of the incoming preference first. Unexpected encoding
chains remain rejected rather than restoring unbounded decompression. Tests
exercise actual routes, fragmented gzip, header ownership, and structured errors
for unnegotiated encodings.

### Cleanup errors overriding protocol responses

A transport `aclose()` failure could become a plain 500, replacing known 401,
403, or 429 statuses and retry headers. Cleanup errors are now sanitized HTTP
errors on read paths; final close failures are logged without replacing an
already selected protocol response. Cancellation remains independently propagated.

Regression cases cover EOF and early bounded-read cleanup, success/error
statuses, size limits, retry headers, and cancellation without secret leakage.

### False-positive qualification results

The response-cancellation harness could accept an empty body, heartbeat-only
stream, or already-completed response as an interruption. It now requires a
parsed nonterminal response event, checks all complete events in the received
chunk, and rejects premature terminal/error events. This still proves only
client close followed by recovery, not GUI or upstream cancellation timing.

The active-drain fixture previously completed after a fixed delay, which could
expire before a delayed `docker stop`. Completion now waits for the relay listener
to disappear during shutdown, and the observer requires an explicit synchronized
completion marker. A successful revised rehearsal was run offline.

The previous six-request/deployment records remain historical observations under
their original harnesses. They are not retroactively upgraded to the stronger
criteria. No additional live inference was run during this review.

### Existing helpers affected by production onboarding/deployment

- The lifecycle menu continued to recreation after a failed Docker build. Every
  Docker action now checks its native exit code and prevents dependent calls on
  failure. Mock tests verify failures never trigger a later mutation or success
  report. The interactive menu is not invoked by those tests.
- The CLI config writer rejected fresh/empty input and failed to remove commented
  managed table headers, risking duplicate TOML definitions. It now handles empty,
  commented and ordinary quoted headers, preserves desktop provider identities,
  removes the legacy selector only at the root, and refuses ambiguous syntax
  before writing. Tests operate only on synthetic strings; user configuration
  was not read or modified.

These two defects predated the hardening diff but directly affected the supported
production setup paths. The fixes retain confirmation boundaries and do not add
automatic desktop migration.

### Dependency drift checks

`uv sync --frozen` does not verify that `pyproject.toml` agrees with the lockfile.
Build targets now first run `uv lock --check --offline`. The offline test runner
also checks installed locked dependencies without installing anything. It excludes
editable project freshness because current source is intentionally mounted into
the development image. Synthetic drift/missing-environment tests verify failure
without changing the lockfile or creating an environment.

## Outdated guidance removed or corrected

- Production restart/source-mount instructions now distinguish immutable
  production images from the separate editable development stack.
- Automatic HTTPX decompression and unbounded-chunk descriptions were replaced
  with the bounded decoder and explicit encoding-negotiation contract.
- The inaccurate claim that `--frozen` detects dependency drift was removed.
- The bundled model-switch skill no longer publishes a fixed "available/latest"
  model table or invented context sizes. It discovers advertised IDs and asks
  about ambiguous family-only selections.
- Host command-line OAuth-token extraction was removed from the README
  diagnostic example; ordinary model listing uses the loopback relay.
- Local ignored Copilot instructions were updated to the same production/test
  workflow without publishing or replacing unrelated local customization.
- Ruff cache and local agent instructions are excluded from Docker build context;
  Ruff cache is also ignored by Git. Existing caches and environments were not deleted.

Dated release/review records, the legacy provider ID, helper filenames, and
compatibility routes remain intentionally present. Age alone is not evidence
that an identifier, fallback, or user-owned artifact is safe to delete.

## Validation and deployment boundary

Final checks passed:

- 689 Python tests with 99% package coverage.
- 126 PowerShell assertions: 97 desktop, 15 CLI configuration, 14 lifecycle.
- Ruff, PowerShell syntax, documentation examples and 56 local links.
- Offline manifest/lock consistency and installed dependency checks.
- Production/development Compose checks, including a synthetic checkout without `.env`.
- Repository skill validation.
- Non-root/runtime-only production smoke, idle shutdown, and the synchronized
  active-stream drain rehearsal.
- Three independent review scopes: runtime; scripts/helpers; packaging/docs.
  All actionable findings were resolved and affected review coverage refreshed.

No tests were skipped, deleted, or weakened to produce a passing result.

The review production artifact is built separately from the deployed image.
Its local tag is `copilot-cli-relay:review-20261026`, with image ID
`sha256:a188da1fc930c714b37460093f3d49ef26de2461338fef5f97d7394c149ecf5c`.
All 14 installed module hashes match the reviewed source. At review completion,
the running image remained the previously recorded `sha256:40fba3f19c1de7fc7cafe68b92881c88bcd272cff2cc97282f397fb93ed393bd`.
Locked dependencies were seeded from the existing approved cache, with build
RUN steps offline. The clean-download TLS issue is not considered resolved.
Neither the live relay, app configuration, credentials, nor permission settings
were changed. Applying runtime fixes requires a separately approved recreation
using the reviewed artifact, followed by deployment checks.

This was not a hosted-CI run, clean remote dependency restore, or a comprehensive
dependency vulnerability scan. Native checkpoint support and the package-host
TLS blocker retain their separately recorded limitations.

## Subsequent compaction configuration follow-up

The later user reports confirm manual `/compact` completion in both clients.
The standalone native endpoint limitation is not a general compaction blocker.
Setup helpers now explicitly select harness-managed local compaction; see
[configuration and acceptance](compaction.md). This client-only follow-up does
not deploy the reviewed runtime image or reinterpret the earlier artifact tests.

## Subsequent approved deployment

The user later authorized deploying the reviewed image. The
[refresh record](deployment-2026-10-26.md#reviewed-image-refresh) records the exact
running artifact, repeated operational checks, and retained rollback image.
The user subsequently confirmed manual `/compact` in both CLI and desktop.
Post-compaction fact retention, tool continuity, and automatic context-limit
triggering remain unverified; earlier inference evidence is not automatically
transferred to the new artifact.
