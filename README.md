<h1 align="center">
  <img src="assets/logo.svg" alt="claude-copilot-cli-relay" width="900">
</h1>

<p align="center">
  🤖 Claude Code &nbsp;·&nbsp; 🛰️ GitHub Copilot Enterprise
</p>

<p align="center">
  <em>A small Python reverse proxy that lets <strong>Claude Code</strong> talk to <strong>GitHub Copilot Enterprise</strong>'s native Anthropic Messages endpoint — swap the auth header, keep the protocol. No separate Anthropic API key needed.</em>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.14+-blue?logo=python&logoColor=white">
  <img alt="Tests" src="https://img.shields.io/badge/tests-173%20passing-brightgreen?logo=pytest&logoColor=white">
  <img alt="Coverage" src="https://img.shields.io/badge/coverage-99%25-brightgreen">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-blue">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows-blue?logo=windows11&logoColor=white">
</p>

<p align="center">
  <a href="https://www.anthropic.com/claude"><img alt="Anthropic Claude" src="https://img.shields.io/badge/Anthropic-Claude-D97757?logo=anthropic&logoColor=white"></a>
  <img alt="bridge" src="https://img.shields.io/badge/-%E2%87%84-lightgrey">
  <a href="https://github.com/features/copilot"><img alt="GitHub Copilot" src="https://img.shields.io/badge/GitHub-Copilot-181717?logo=githubcopilot&logoColor=white"></a>
</p>

---

> ## ⚠️ Disclaimer
>
> **Unofficial, unsupported, proof-of-concept.** This project is an independent experiment and is **not affiliated with, endorsed by, or supported by Microsoft, GitHub, or Anthropic**. None of those organizations have reviewed, blessed, or sanctioned it.
>
> It is **not a product**. There is no warranty, no SLA, no support, and no guarantee of fitness for any purpose. APIs, headers, model availability, and tenant behavior on the upstream services can change at any time and break this proxy without notice.
>
> **Use at your own risk.** You are solely responsible for ensuring that your use of GitHub Copilot through this proxy complies with your Copilot subscription terms, your employer's acceptable-use policies, and any applicable laws. The authors and contributors accept **no liability** for account suspension, data loss, billing surprises, security incidents, or any other damages arising from use of this software. See [`LICENSE`](LICENSE) for the full no-warranty / no-liability terms.

---

A small Python reverse proxy that runs in Docker.It forwards Anthropic Messages API requests from Claude Code to Copilot's native `/v1/messages` endpoint, swapping the auth header. **No protocol translation** — Copilot speaks the Anthropic protocol natively for Claude models.

## How it works

```
┌─────────────┐         ┌──────────────┐         ┌────────────────────────┐
│ Claude Code │ ──────▶ │  this proxy  │ ──────▶ │     GitHub Copilot     │
│    (CLI)    │         │  127.0.0.1   │         │ api.githubcopilot.com  │
│             │ ◀────── │    :4141     │ ◀────── │     /v1/messages       │
└─────────────┘         └──────┬───────┘         └────────────────────────┘
                               │
                               │ • adds  Authorization: Bearer gho_…
                               │ • adds  Copilot-Integration-Id: copilot-developer-cli
                               │ • strips client auth + hop-by-hop headers
                               │
                               ▼
                    Windows Credential Manager
                    (OAuth token from `copilot` CLI login)
```

Same Anthropic Messages protocol end-to-end — the proxy's only job is to swap the client's (absent) auth for the GitHub OAuth bearer Copilot Enterprise expects, and to add the `Copilot-Integration-Id` header that unlocks Claude models on the tenant.

Three things make this work cleanly:

1. The `copilot` CLI's OAuth token (in Windows Credential Manager) authenticates directly against `api.githubcopilot.com` — **no session-token exchange needed**.
2. Copilot exposes a native Anthropic Messages endpoint at `/v1/messages` that returns proper Anthropic SSE — **no API translation needed**.
3. Setting `Copilot-Integration-Id: copilot-developer-cli` is what unlocks Claude models on Copilot Enterprise (`vscode-chat` only exposes GPT models for this user's tenant).

## Quick start (Windows)

Requires: Docker Desktop, PowerShell 7+ (`pwsh`), and a working `copilot` CLI login (`copilot` then sign in).

```powershell
# 1. Extract your Copilot OAuth token from Credential Manager → .env
pwsh scripts\extract-token.ps1

# 2. Start the proxy
docker compose up --build

# 3. In another terminal: verify
curl http://localhost:4141/healthz
curl http://localhost:4141/v1/models
```

Then point Claude Code at the proxy by creating `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:4141",
    "ANTHROPIC_AUTH_TOKEN": "sk-dummy",
    "ANTHROPIC_MODEL": "claude-opus-4-7",
    "ANTHROPIC_SMALL_FAST_MODEL": "claude-sonnet-4-6"
  },
  "model": "claude-opus-4-7",
  "effortLevel": "medium"
}
```

Then run `claude` as usual.

### Lifecycle helper

Interactive menu wrapping the common `docker compose` flows. Run it with no
arguments and pick a numbered action:

```powershell
pwsh scripts\proxy.ps1
```

```
claude-copilot-cli-relay — pick an action:
  1) start     Start container (build only if image missing)
  2) stop      Stop and remove container + network
  3) restart   Restart to pick up src/ edits (starts if down)
  4) status    Show container status + port bind
  5) rebuild   Rebuild image and recreate container
  6) health    GET /healthz on localhost:4141
  7) quit      Exit without doing anything
```

For scripted use, prefer the underlying `docker compose` commands directly.

### Picking the default model

Claude Code resolves the active model from three places, in this order of precedence:

1. **Top-level `"model"` in `settings.json`** — what the UI shows as your default and what `/model` switches between in-session.
2. **`ANTHROPIC_MODEL` env var** — the fallback used when no top-level `"model"` is set.
3. **`ANTHROPIC_SMALL_FAST_MODEL` env var** — used for cheap/quick background calls (title generation, tool-name guesses, etc.). Point this at a Haiku- or Sonnet-tier model so background traffic doesn't burn Opus quota.

For consistency, set both the top-level `"model"` and `ANTHROPIC_MODEL` to the same id so a stale env var can't silently override your settings.

A few things to know about the IDs:

- **Use the dash form** (`claude-opus-4-7`, not `claude-opus-4.7`). The proxy canonicalizes `/v1/models` output to dash form because that's the shape Claude Code recognizes as a known Anthropic model — using it unlocks the right request shape (adaptive thinking, etc.). Both forms work upstream, but dash is what you want here.
- **`effortLevel`** is Claude Code's reasoning-effort knob. Valid values are `low`, `medium`, `high`. The proxy clamps per-model: Opus 4.7 currently only accepts `medium`, and Haiku 4.5 doesn't support reasoning effort at all (the field is stripped). If you set `high` for an Opus 4.7 default, the proxy quietly downgrades it to `medium` rather than letting the request 400.
- **1M context:** Copilot exposes 1M-context variants for Opus 4.6 and 4.7 only — `/v1/models` advertises them as `claude-opus-4-6-1m` and `claude-opus-4-7-1m-internal` (dash form, what Claude Code's `/model` validation accepts). The proxy converts these ids to the dot form (`claude-opus-4.6-1m` / `claude-opus-4.7-1m-internal`) only at the last hop before forwarding upstream — Copilot returns `model_not_supported` for the dash form on these specific ids, while accepting both forms for every other model. When Claude Code's hardcoded "Opus 4.7 (1M context)" picker tier sends the standard `claude-opus-4-7` id plus the `context-1m-2025-08-07` beta header, the proxy auto-rewrites the model id to the 1M variant so you actually get 1M context (Copilot rejects the beta header itself; the `-1m` model id is the real switch). To pin a 1M variant as your session default, set `"model": "claude-opus-4-7-1m-internal"` in `settings.json`. Sonnet has no 1M variant on this tenant — picker tier "Sonnet (1M context)" silently downgrades to 200K because there's nothing to remap to.
- **Switching mid-session:** `/model claude-sonnet-4-6` inside Claude Code changes the model for the current conversation without editing `settings.json`.

Optional env vars to consider adding to the `"env"` block:

- `"DISABLE_NON_ESSENTIAL_MODEL_CALLS": "1"` — skip the small/fast background calls entirely if you don't want any haiku traffic.
- `"CLAUDE_CODE_ATTRIBUTION_HEADER": "0"` — drop the `X-Claude-Code-…` attribution header from outbound requests.

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `COPILOT_GITHUB_TOKEN` | — | **Required.** Set by `extract-token.ps1`. |
| `COPILOT_PROXY_PORT` | `4141` | Read only by the `python -m claude_copilot_cli_relay` entrypoint. The Docker `CMD` hardcodes `--port 4141`, so changing this in `.env` has no effect when running via `docker compose` — change the published port in `docker-compose.yml` instead. |
| `COPILOT_PROXY_HOST` | `127.0.0.1` | Read only by the `python -m claude_copilot_cli_relay` entrypoint, which the Docker CMD does **not** use (it invokes `uvicorn` directly). Only relevant if you bypass the default CMD inside the container. |
| `COPILOT_API_BASE` | `https://api.githubcopilot.com` | Override upstream base. **Must be `https://`** unless `COPILOT_ALLOW_INSECURE_API_BASE=1` is also set (mocks/tests only — the proxy injects the OAuth bearer into every outbound request). |
| `COPILOT_ALLOW_INSECURE_API_BASE` | `0` | Set to `1` to permit non-https `COPILOT_API_BASE` for local mocks. Default refuses. |
| `COPILOT_INTEGRATION_ID` | `copilot-developer-cli` | **Do not change** unless you know your org needs a different value |
| `COPILOT_EDITOR_VERSION` | `claude-copilot-cli-relay/<package version>` | Sent as `Editor-Version` and `User-Agent`. Defaults to the value of `__version__` in `src/claude_copilot_cli_relay/__init__.py` (currently `0.1.0`). |
| `LOG_LEVEL` | `info` | Set to `debug` for header-level debug |
| `LOG_BODIES` | `0` | Set to `1` to log redacted request/response bodies. **Bodies contain your source code; off by default.** |

## Troubleshooting

- **`/v1/models` returns no Claude models:** your `COPILOT_INTEGRATION_ID` is wrong, or your org policy doesn't include Claude models for your account.
- **401 from upstream:** your token rotated. Re-run `pwsh scripts\extract-token.ps1` and `docker compose restart proxy`.
- **`COPILOT_GITHUB_TOKEN is required`:** run `extract-token.ps1` first; it writes `.env`.
- **Source edits not picked up:** the container runs uvicorn without `--reload`. Run `docker compose restart proxy` after editing `src/`. (Bind-mount file events on Windows are also flaky, so the restart is the reliable path.)
- **`Unable to connect to API (ConnectionRefused)` in Claude Code:** the container isn't running. `docker compose up -d proxy` brings it back. The compose file uses `restart: unless-stopped`, so this should only happen after a clean `docker compose down`.

## Development

**Everything happens in Docker.** Python is not required on the host — there's no host-side `venv`, no host-side `uv`, no host-side `pip install`. The Docker image is the *only* environment the project runs in, including for tests and for managing dependencies. This keeps dev, test, and prod identical and means `git clone` + Docker Desktop is the entire prerequisite list.

```powershell
# Run the proxy (Docker)
docker compose up -d --build proxy

# Tail logs
docker compose logs -f proxy

# Restart to pick up source edits (src/ and tests/ are bind-mounted read-only)
docker compose restart proxy

# Stop
docker compose down

# Run unit tests
docker compose run --rm proxy uv run pytest tests/unit -q

# Lint
docker compose run --rm proxy uv run --with ruff ruff check src tests

# Run a single test file or test
docker compose run --rm proxy uv run pytest tests/unit/test_proxy.py -q
docker compose run --rm proxy uv run pytest tests/unit/test_proxy.py::test_models_filters_and_canonicalizes -q

# Coverage report
docker compose run --rm proxy bash -c "uv run --with coverage coverage run --source=src/claude_copilot_cli_relay -m pytest tests/unit -q && uv run --with coverage coverage report"

# Open a shell inside the running container
docker compose exec proxy bash
```

### Adding or upgrading a dependency

The lockfile (`uv.lock`) is committed and the Dockerfile uses `uv sync --frozen`, so any change to `pyproject.toml` requires a matching `uv lock` regeneration before the next build will succeed. Both steps run in throwaway containers — nothing leaks onto the host.

```powershell
# 1. Edit pyproject.toml (add/remove/bump a package).

# 2. Regenerate the lockfile in a throwaway container that mounts the repo.
docker run --rm -v "${PWD}:/work" -w /work --user root claude-copilot-cli-relay:dev uv lock

# 3. Rebuild the proxy image with the new lockfile.
docker compose build proxy

# 4. Run the tests to confirm nothing broke.
docker compose run --rm proxy uv run pytest tests/unit -q
```

If you don't yet have the `claude-copilot-cli-relay:dev` image locally, replace step 2 with the standalone uv image: `docker run --rm -v "${PWD}:/work" -w /work ghcr.io/astral-sh/uv:python3.14-bookworm-slim uv lock`.

## Security notes

- The OAuth token is read fresh from `.env` at container start; the proxy never persists it.
- `extract-token.ps1` writes `.env` with a hardened ACL: inheritance is disabled and dropped, then explicit ACEs are added for the current user and `NT AUTHORITY\SYSTEM`. In practice the resulting on-disk DACL also includes `BUILTIN\Administrators` with FullControl when the script is run by an account in that group; the script does not strip this. It refuses to write the token if the ACL hardening fails. On a single-user dev box this is "owner-only" in practice; on a host with multiple local Administrators, anyone in that group can read the file.
- The proxy binds only to `127.0.0.1:4141` on the host. Not reachable from the network.
- The container runs as a non-root user (UID 1000).
- Inbound `Authorization`, `x-api-key`, `proxy-authorization`, `Cookie`, and `User-Agent` headers from clients are stripped before any upstream forward (regression-tested). Upstream `Set-Cookie` is stripped from responses returned to the client.
- Body logging is **off by default**. When enabled, secrets are redacted: GitHub tokens (classic `gh[ousrp]_` and fine-grained `github_pat_`), JWTs, `sk-`/`sk-ant-`/`sk-proj-` keys, AWS access key ids, AWS secret access keys when prefixed by their conventional key name, Slack tokens (`xox[baprs]-`), Stripe live/restricted keys (`sk_live_/rk_live_`), Google API keys (`AIza…`), generic `Bearer …` tokens, the values of `Authorization` / `x-api-key` / `api-key` / `proxy-authorization` header lines (both raw `Header: value` form and quoted dict-/JSON-repr form), and JSON keys named `password` / `api_key` / `access_token` / `auth_token` / `secret` / `client_secret` / `refresh_token` / `private_key`. Exception messages on every error path also go through the same redaction before being logged or returned to the client. **Bodies still contain the user's source code** — redaction is best-effort, not a substitute for treating logs as sensitive.
- `.env` and `.env.*` are gitignored and dockerignored (with `!.env.example` exception). Verify with `git check-ignore -v .env` before your first commit.

## License

[MIT](LICENSE).
