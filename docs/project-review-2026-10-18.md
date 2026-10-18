# Project review: October 18, 2026

The initial review identified four inherited runtime issues, with no newly
identified regression in the Codex app changes. All four reported issues are
now fixed in the working tree after an explicitly authorized follow-up.
The relay has since been restarted with the approved fixes; post-restart
upstream health and model-catalog checks passed.

## Original review scope and evidence

Reviewed the relay's Python modules and unit tests, Windows configuration and
lifecycle helpers, Docker/build/dependency metadata, documentation, example
configuration, branding, and the Claude-only model-switch skill. An independent
reviewer inspected the Python implementation and reproduced the findings below.
This was not a dependency vulnerability scan or a new live-upstream qualification.

- 310 Python tests passed; Ruff passed; package coverage is 99%.
- `proxy_shared.py` has 100% coverage. Four lines in the retired-Claude-model
  compatibility helper remain uncovered.
- 38 PowerShell assertions passed, including the updated app-provider display name.
- All six PowerShell files and 14 documentation command blocks passed syntax parsing.
- SVG XML and `git diff --check` passed.
- Generated app TOML, three documentation TOML/JSON snippets, package/lock
  metadata consistency, and 35 local documentation links/anchors passed.
- An independent reviewer ran 28 synthetic reproduction/control checks for the
  findings below, without changing the maintained test suite.

Python checks and reproductions used the existing Docker image pinned by image
ID, networking disabled, and read-only source/test mounts. No real credentials
or user configuration were used for those checks.

The [app guide](codex-desktop.md#recorded-validation) records the earlier GUI
tool loop, updated-relay bundled-engine tests, and user-confirmed post-restart
text reply separately. GUI follow-up, cancellation/recovery, and reopening
were not newly tested during this review.

## Runtime findings and resolutions

All four were P2 findings inherited from the existing implementation, not caused
by the app integration. The following records the original synthetic
reproductions and the subsequent fixes.

### 1. Truncation can defeat field-based secret redaction

Locations: [`logging_setup.py`](../src/copilot_cli_relay/logging_setup.py)
(`redact_bytes`, `redact_text`), [`proxy_shared.py`](../src/copilot_cli_relay/proxy_shared.py)
(`read_bounded`), and the route error-response builders.

A JSON secret whose closing quote fell outside the 16 KiB logging cutoff could
survive redaction. The same problem occurred when the 32 KiB upstream error-body
read cap removed a closing quote. It was reproduced in returned Claude and
Codex errors for streaming and non-streaming requests, **with `LOG_BODIES=0`**.
A preceding large redactable field can shrink enough to bring the exposed
synthetic value inside the final 300-character display.

The 300-character display slice is applied after redaction and was not itself
the failing boundary in the control test. The problem is truncating input before
the field-based redactor has enough information.

**Fixed:** raw bodies reaching the 16 KiB logging limit or 32 KiB error-read cap
are omitted before redaction or data-URI shortening. Exact-cap input is treated
conservatively without reading another chunk. Complete in-limit input is
redacted before display truncation; redactor failures emit only a static
omission marker. The bounded reader appends only the remaining allowed bytes.

Tests cover both protocols, streaming and non-streaming requests, logging on
and off, below/exact/above-cap bodies, fragmented and large chunks, unread
tails, invalid/multibyte UTF-8, data URIs, and redactor failure.

Remaining limit: the existing pattern redactor is not a guarantee for arbitrary
malformed or already-truncated upstream text below the cap, or unknown secret
formats. Diagnostics remain sensitive even with these fixes.

### 2. Synthetic compaction can report an empty success

Location: [`codex_proxy.py`](../src/copilot_cli_relay/codex_proxy.py)
(`proxy_codex_responses_compact`).

After native compaction returns 404, a fallback HTTP 200 containing
`{"status":"failed","output":[]}` was wrapped as a successful
`response.compaction` with empty output. Incomplete responses and non-object
JSON reproduced the same problem: failure information was discarded.

**Fixed:** a synthetic success requires a successful HTTP status, a completed
Response, completed assistant messages, and nonblank `output_text`. Every output
item is checked, so malformed neighbors, refusals, incomplete reasoning, or
dangling tool calls cannot ride alongside a valid summary. Invalid results
return a static OpenAI-shaped 502 without echoing upstream failure details.
Native passthrough, fallback only after native 404, and upstream HTTP error
status/header propagation are preserved.

The final guard also checks known nested reasoning, citation annotations,
logprobs, and supplied usage fields. Valid optional/null usage is preserved.
Non-finite numbers and overflow are rejected during JSON parsing, and
serialization/encoding failures return static protocol-shaped 502 errors.
Field checks follow the [Responses reference](https://developers.openai.com/api/reference/resources/responses/methods/create);
this is not a promise of compatibility with every future output variant.

The ordinary-summary fallback still needs separate qualification as a
long-session checkpoint or v2 compaction replacement.

### 3. Malformed catalogs can produce HTTP 500 and misleading health

Locations: [`claude_proxy.py`](../src/copilot_cli_relay/claude_proxy.py)
and [`codex_proxy.py`](../src/copilot_cli_relay/codex_proxy.py)
(model-list and health handlers).

An upstream `{"data":null}` or `{"data":123}` produced HTTP 500 from both model
catalog routes. A truthy non-string Claude `vendor` also failed. Health handlers
retained `upstream_ok=true` after a counting error because the flag was set
before shape validation finished. Valid-catalog controls passed.

**Fixed:** both model routes require an object containing a `data` array,
returning protocol-shaped 502 errors for invalid containers. Health reports
`upstream_ok=false` and zero matching rows for those failures while retaining
the actual upstream HTTP status. Success is assigned only after counting.
Non-string Claude vendors are safely excluded.

Individual rows retain the existing tolerant filtering/defaults. A valid empty
or all-skipped list can remain upstream-healthy with zero matches. Counts
describe advertised matching rows, not deduplicated selectable models. The app
helper's additional catalog/count/selection checks remain necessary.

### 4. Claude relay-owned headers can collide case-insensitively

Location: [`headers.py`](../src/copilot_cli_relay/headers.py)
(`build_claude_outbound_headers`).

Lowercase incoming `copilot-integration-id`, `editor-version`, and `x-request-id`
survived alongside the injected title-case keys, yielding two outbound values
for each HTTP header. This could affect upstream identity selection and request
correlation. Codex's rebuilt header set produces one authoritative value;
Authorization replacement remains correct in both protocols.

**Fixed:** incoming relay-owned identity/correlation headers are removed
case-insensitively before authoritative values are injected. Tests inspect the
constructed HTTPX request's header value lists, including simultaneous casing
variants, explicit/generated request IDs, and dynamic `Connection` stripping.
Allowed client headers and Anthropic version handling remain intact.

## Initial documentation pass

- Updated the README, architecture, app guide, glossary, logo text, package
  description, and helper help/display text for the unified app terminology.
- Documented the shared Responses path, local execution boundary, provider
  precedence, existing provider-ID preservation, and print/check-only setup.
- Distinguished health/catalog checks, GUI evidence, bundled-engine tests, and
  unqualified workflows.
- Corrected token-refresh guidance to recreate the container after `.env`
  changes, including health-response hints and the lifecycle helper's warning.
- Corrected Compose healthcheck comments without changing its behavior.
- Added assertions for the new display name and recreation guidance without
  deleting or weakening existing tests.

That initial pass did not change dependency versions, model defaults, endpoint
routing, authentication, permission policies, or stream-lifecycle logic.

## Authorized runtime remediation

The subsequent approved fixes add 168 regression cases in
[`test_runtime_hardening.py`](../tests/unit/test_runtime_hardening.py).
The first 128 cases plus two strengthened existing expectations produced
103 failures and 27 passing controls before implementation. Independent review
then caught a nested-compaction validation gap; its added reproduction set
produced 35 failures and one passing control before that correction. Additional
phase, encoding, and nullable-usage controls complete the final set.
After the fixes:

- All **478 Python tests** pass.
- Ruff passes; package coverage remains **99%**. The same four legacy
  retired-model compatibility lines remain uncovered.
- The Windows helper and its **38 passing assertions** are unchanged.
- Existing successful compaction fixtures now supply completed assistant
  summaries while retaining their routing, input, optional-field, and usage
  assertions. No tests were deleted, skipped, or weakened.

Automated validation used the pinned local Docker image, disabled networking,
synthetic inputs, and read-only source/test mounts. After a separately authorized
relay restart, all five changed runtime modules matched the approved source
hashes. Both upstream health checks passed, and the Codex catalog contained 19
models including the pilot model. The ChatGPT app was not restarted or
reconfigured, and GUI inference was not re-tested during this deployment.
