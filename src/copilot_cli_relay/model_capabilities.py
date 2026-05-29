"""Upstream model-capability cache used to clamp Claude reasoning-effort.

Copilot's `/models` response advertises, per model, which reasoning-effort
values it accepts (`capabilities.supports.reasoning_effort`, e.g. ["medium"]).
Hardcoding that in a static table means every new/changed model risks a 400
until someone edits the table. This module fetches and caches those facts so
the effort clamp adapts automatically; the static table in `claude_proxy`
remains only as an offline fallback when `/models` is unreachable.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from .config import Settings
from .headers import build_claude_outbound_headers
from .logging_setup import logger, redact_text

# How long a fetched /models snapshot stays fresh before a refresh is attempted.
CAPS_TTL_SECS = 300.0
# Shorter back-off after a failed refresh so a transient outage self-heals
# quickly without hammering a persistently failing upstream every request.
CAPS_ERROR_BACKOFF_SECS = 30.0
# Bounded timeout for the COLD-START fetch (when no snapshot exists yet and a
# request blocks on it). Healthy /models answers in well under a second; this
# caps the worst-case added latency if /models is slow on the very first call.
# Once any snapshot exists, refreshes happen in the background (see `get`) and
# never block a request, so they use the longer timeout below.
CAPS_COLD_FETCH_TIMEOUT_SECS = 5.0
CAPS_BACKGROUND_FETCH_TIMEOUT_SECS = 30.0

# {model_id -> allowed reasoning_effort values | None}. A set means "clamp to
# these"; None means the model advertises no reasoning_effort support and the
# field should be stripped. A model id ABSENT from the mapping is "unknown" —
# callers fall back to the static table / default rather than treating it as
# strip-or-clamp.
EffortCaps = dict[str, "set[str] | None"]


def build_effort_snapshot(payload: object) -> EffortCaps:
    """Build an :data:`EffortCaps` mapping from a `/models` JSON payload.

    Each id is stored lowercased in BOTH dot and dash forms (`claude-opus-4.8`
    and `claude-opus-4-8`) so a lookup by either shape hits. Malformed entries
    are skipped defensively rather than raising.
    """
    snapshot: EffortCaps = {}
    data = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(data, list):
        return snapshot
    for model in data:
        if not isinstance(model, dict):
            continue
        mid = model.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        caps = model.get("capabilities")
        supports = caps.get("supports") if isinstance(caps, dict) else None
        allowed: set[str] | None = None
        if isinstance(supports, dict):
            raw = supports.get("reasoning_effort")
            if isinstance(raw, list):
                vals = {v.strip().lower() for v in raw if isinstance(v, str) and v.strip()}
                # A present-but-empty list still means "no usable efforts" -> strip.
                allowed = vals or None
        for key in {mid.lower(), mid.replace(".", "-").lower()}:
            snapshot[key] = allowed
    return snapshot


class ModelCapabilityCache:
    """Caches reasoning-effort capabilities with a TTL and single-flight refresh.

    Refresh strategy is stale-while-revalidate: once any snapshot exists, an
    expired snapshot is served immediately and a single background task
    refreshes it, so a slow or hanging `/models` never delays an in-flight
    `/v1/messages` request. Only the cold start (no snapshot yet) blocks, and
    that fetch is bounded by `CAPS_COLD_FETCH_TIMEOUT_SECS`.
    """

    def __init__(self, ttl_secs: float = CAPS_TTL_SECS) -> None:
        self._ttl = ttl_secs
        self._snapshot: EffortCaps = {}
        self._have_snapshot = False
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None

    async def get(self, client: httpx.AsyncClient, settings: Settings) -> EffortCaps:
        """Return a capability snapshot.

        Never raises and never blocks once a snapshot exists. On a stale
        snapshot the current (stale) value is returned immediately while a
        background refresh runs. Only the cold start awaits a bounded fetch; on
        any error it returns an empty snapshot so the caller uses its static
        fallback.
        """
        if time.monotonic() < self._expires_at:
            return self._snapshot
        if self._have_snapshot:
            # Stale-while-revalidate: serve the old snapshot, refresh in background.
            self._ensure_background_refresh(client, settings)
            return self._snapshot
        # Cold start: block (bounded) so the first request gets real caps.
        async with self._lock:
            if self._have_snapshot or time.monotonic() < self._expires_at:
                return self._snapshot
            await self._refresh_locked(client, settings, CAPS_COLD_FETCH_TIMEOUT_SECS)
            return self._snapshot

    async def aclose(self) -> None:
        """Cancel any in-flight background refresh (call on app shutdown).

        Avoids a cosmetic "Task was destroyed but it is pending" warning if a
        refresh is still running when the shared http client is closed.
        """
        task = self._refresh_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - shutdown cleanup must not raise
                pass

    def _ensure_background_refresh(self, client: httpx.AsyncClient, settings: Settings) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        # Push expiry forward so we don't spawn a refresh task per request while
        # one is already in flight (or until the back-off window elapses).
        self._expires_at = time.monotonic() + min(self._ttl, CAPS_ERROR_BACKOFF_SECS)
        self._refresh_task = asyncio.create_task(self._background_refresh(client, settings))

    async def _background_refresh(self, client: httpx.AsyncClient, settings: Settings) -> None:
        async with self._lock:
            await self._refresh_locked(client, settings, CAPS_BACKGROUND_FETCH_TIMEOUT_SECS)

    async def _refresh_locked(
        self, client: httpx.AsyncClient, settings: Settings, timeout: float
    ) -> None:
        try:
            snapshot = await self._fetch(client, settings, timeout)
        except Exception as exc:  # noqa: BLE001 - a caps refresh must never break a request
            logger.warning("model caps refresh failed: %s", redact_text(str(exc)))
            self._expires_at = time.monotonic() + min(self._ttl, CAPS_ERROR_BACKOFF_SECS)
            return
        self._snapshot = snapshot
        self._have_snapshot = True
        self._expires_at = time.monotonic() + self._ttl

    async def _fetch(
        self, client: httpx.AsyncClient, settings: Settings, timeout: float
    ) -> EffortCaps:
        headers = build_claude_outbound_headers(
            {},
            bearer_token=settings.github_token,
            integration_id=settings.integration_id,
            editor_version=settings.editor_version,
        )
        headers["Accept"] = "application/json"
        resp = await client.get(f"{settings.api_base}/models", headers=headers, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"/models returned status {resp.status_code}")
        return build_effort_snapshot(resp.json())
