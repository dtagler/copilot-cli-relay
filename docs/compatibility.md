# Compatibility and release qualification

Support is version-scoped. A passing unit suite or readiness endpoint does not
qualify the desktop GUI, tools, history recovery, or compaction.

## Evidence matrix

| Combination | Evidence | Status |
|---|---|---|
| Windows ARM64 `OpenAI.Codex` 26.1013.4894.0, engine 0.163.0-alpha.10.2, `gpt-6-astra`, October 18, 2026 | Original GUI tool loop and separate engine/restart observations in the [desktop guide](codex-desktop.md#recorded-validation) | Historical, bounded evidence |
| Desktop GUI checklist reported by the user, October 26, 2026 | Provider label, actual fixture read, follow-up without rereading, streamed stop/recovery, and reopening all passed | User-confirmed five-check pass, not independently observed; [evidence boundaries](remaining-qualification.md#user-assisted-gui-acceptance) |
| 0.6.0 full-project review before the compaction follow-up, October 26, 2026 | 689 Python tests, 99% package coverage, 126 PowerShell assertions across three suites; lint/docs/Compose and lock/environment checks | Passed locally; subsequently deployed in the refresh below; hosted CI not run |
| Compaction configuration follow-up, October 26, 2026 | 689 Python tests, 99% coverage, 130 PowerShell assertions, four generated configurations parsed as TOML; lint/docs and lock/environment checks | Offline checks passed; client settings updated, reload and post-change routing not verified |
| 0.6.0 immutable Linux ARM64 image, October 26, 2026 | Non-root, no development packages or uv, offline liveness, graceful idle and active-stream shutdown; all 14 module hashes match source | Passed with cached dependencies; fresh restore blocked by TLS |
| 0.6.0 live relay with `gpt-6-astra`, October 26, 2026 | Six approved requests: completed streaming, fixture tool call/result replay, follow-up, client-close/recovery | Passed; [deployment evidence](deployment-2026-10-26.md) |
| Windows ARM64 `OpenAI.Codex` 26.1021.3705.0, engine 0.164.0-alpha.11.1 | Installed/running package match and prerequisites verified earlier that day; subsequent five-check GUI pass reported by the user | Prerequisites verified; GUI user-confirmed, without re-inventory at confirmation |
| Bundled engine 0.164.0-alpha.11.1 with relay-shaped catalog | Unique marker consumed with isolated configuration; 15 models including `gpt-6-astra` | Catalog/config compatibility passed; [details and GUI checklist](remaining-qualification.md) |
| Previous 0.5.0 image with rollback adapter, October 26, 2026 | Starts non-root/read-only; legacy health route works with new deployment constraints | Offline rehearsal passed; no live rollback was needed |
| Manual `/compact` in Codex CLI and the ChatGPT Codex app, October 26, 2026 | User separately reported completion in both clients before the explicit configuration change | User-confirmed pass; route, automatic triggering, and post-change tool continuation not independently verified |
| Manual `/compact` after the configuration edit, October 26, 2026 | User reported another successful test before container refresh, without specifying the client | User-confirmed completion; reload, routing, and tool continuity not independently verified |
| Reviewed production image refresh, October 26, 2026 | Exact reviewed image deployed; 14 source hashes match; repeated Compose/smoke/shutdown checks and live liveness/readiness pass | Operational checks passed; [refresh and tag alignment](deployment-2026-10-26.md#reviewed-image-refresh) |
| Post-refresh manual `/compact` in CLI and desktop, October 26, 2026 | User explicitly confirmed successful manual completion in both clients | User-confirmed pass; routing, automatic triggering, retained facts, and tool continuity remain separately unverified |
| Desktop duplicate output, October 26, 2026, app 26.1021.3705.0 / engine 0.164.0-alpha.11.1 | User reports duplicate full answers in new and existing chats, including after restart; inspected turns have one persisted answer and matching completion ID; CLI unaffected in the reported workflow | Known compatibility issue, presumed desktop-side; [evidence and limits](codex-desktop.md#known-issue-duplicate-desktop-output) |
| Hosted CI on commit `2407760`, October 26, 2026 | Windows helper job and Linux development build/Python checks passed; Linux Compose check required a missing `.env` despite `--no-env-resolution`, preventing production image checks | Failed; validator now uses isolated synthetic env_file, with 38 local regression assertions; corrected hosted rerun still required before release |
| Standalone native compact API through the live relay with `gpt-6-astra` | Bounded synthetic probe returned HTTP 404; no continuation attempted | Legacy API limitation, not a blocker for harness `/compact`; synthetic fallback remains off |
| Windows x64 and other OS/app layouts | Separate client qualification | Not established by the ARM64 observations |

Do not convert pending rows to passed from unrelated tests. Update exact versions,
dates, artifact IDs, tested capabilities, and sanitized outcomes after each run.
Keep account identifiers, credentials, prompts containing private code, and raw
transcripts out of committed evidence.

The [full-project review](project-review-2026-10-26.md) distinguishes the latest
reviewed source from the earlier deployed artifact. Earlier live results remain
evidence for that recorded artifact, not automatic qualification of later fixes.

## Optional synthetic inference qualification

`scripts/qualify-responses.py` is plan-only unless `--run-inference` is supplied.
It sends six synthetic, potentially billable requests: text completion, an
allowlisted fixture tool call, fixed tool-result replay, follow-up, transport
cancellation, and a fresh recovery request. It never executes model-generated
commands or reads user files. It does not claim GUI or checkpoint qualification.

After explicit approval, use the development image in the existing relay's
network namespace. Pin its local image ID and run the offline dependency/source
checks first. Mount only the current harness and runtime source so an older
development image cannot silently run a superseded qualification implementation.
No credentials or user configuration are mounted:

```powershell
# Requires approval for inference and the exact model. Do not substitute a model.
$image = docker image inspect copilot-cli-relay:dev-hardening --format '{{.Id}}'
if ($LASTEXITCODE -ne 0 -or $image -notmatch '\Asha256:[0-9a-f]{64}\z') { throw 'A validated local development image is required.' }
pwsh scripts/test.ps1 -Image $image
if ($LASTEXITCODE -ne 0) { throw 'Current source/dependency checks failed.' }
docker run --rm --pull never --network container:copilot-cli-relay --read-only --tmpfs /tmp:rw,nosuid,nodev,size=32m --mount "type=bind,source=${PWD}/src,target=/app/src,readonly" --mount "type=bind,source=${PWD}/scripts/qualify-responses.py,target=/app/scripts/qualify-responses.py,readonly" $image /app/.venv/bin/python scripts/qualify-responses.py --model gpt-6-astra --run-inference
```

Run plan-only with `--network none` and omit `--run-inference`. Use an already
built, verified image. Summary output contains only test outcomes; do not export
raw request/response bodies. A qualification failure is evidence, not permission
to disable tests or loosen app execution controls.

The harness bounds decoded bytes before buffering SSE lines, limits completed
response checks to 60 seconds and the whole run to 180 seconds, and closes on
terminal completion rather than waiting indefinitely for EOF. The interruption
probe is bounded by the whole-run deadline and the client's idle timeout. It
requires nonterminal response progress before closing. Its client-close/recovery
result is not proof of live upstream cleanup or GUI stop behavior.

## Actual desktop acceptance

With a non-sensitive fixture folder and the least necessary permissions:

1. Record app version, bundled engine, relay version/image ID, platform, model,
   provider ID, enabled capability overrides, and transport.
2. Confirm the selected provider and completed streamed text through relay
   request records, not the model's self-identification.
3. Complete a real local tool call, matching result, and final answer.
4. Ask a context-dependent follow-up without rereading the fixture.
5. Cancel while waiting for upstream headers and during streamed output;
   confirm upstream cleanup and a successful subsequent request.
6. Save, leave, and reopen the conversation. Check retained context and tool IDs.
7. Run harness `/compact` with remote compaction configured as unsupported.
   Verify subsequent requests preserve user constraints, relevant task facts,
   completed work, and tool-result association. Record automatic compaction
   separately. Do not enable the relay's synthetic fallback for this test.
   See [compaction setup and acceptance](compaction.md).
8. Exercise 401/403, 429/Retry-After, outage, oversized input, and slow upstream
   with synthetic fixtures. Never deliberately revoke production credentials.
9. In an approved maintenance window, verify upgrade and rollback to the recorded
   image with configuration and saved conversations preserved.

## Release checklist

- [ ] Final source passes lint, Python tests, coverage, PowerShell tests, docs,
      version consistency, Compose safety checks, and production image smoke.
- [ ] Clean locked build succeeds on the intended architecture; any cache-only
      build or unavailable fresh dependency restore is disclosed.
- [ ] Windows ARM64 desktop and the intended model/bundled-engine tuple are recorded.
- [ ] Newly enabled metadata capabilities have separate evidence.
- [ ] Harness `/compact` and post-compaction continuation are checked for the
      intended configuration; automatic compaction limits are documented.
- [ ] Active-stream shutdown and real desktop recovery are qualified.
- [ ] Independent review covers final code, configuration, untracked artifacts,
      tests and rollout instructions; actionable findings are resolved.
- [ ] Upgrade/rollback and token-renewal procedures are rehearsed safely.
- [ ] Release notes identify changes and compatibility breaks.
- [ ] Publication, deployment, and any host changes have separate authorization.
