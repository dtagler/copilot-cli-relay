"""Tests for dynamic reasoning-effort capability resolution and caching."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from copilot_cli_relay.claude_proxy import (
    _parse_claude_request,
    _resolve_allowed_efforts,
)
from copilot_cli_relay.config import Settings, reset_settings_for_tests
from copilot_cli_relay.model_capabilities import (
    ModelCapabilityCache,
    build_effort_snapshot,
)


def _settings() -> Settings:
    return Settings(
        proxy_port=4141,
        github_token="gho_test",
        api_base="https://upstream.test",
        integration_id="copilot-developer-cli",
        editor_version="copilot-cli-relay/test",
        log_level="info",
        log_bodies=False,
    )


def _models_payload() -> dict:
    return {
        "data": [
            {
                "id": "claude-opus-4.8",
                "vendor": "Anthropic",
                "capabilities": {"type": "chat", "supports": {"reasoning_effort": ["medium"]}},
            },
            {
                "id": "claude-opus-4.7-1m-internal",
                "vendor": "Anthropic",
                "capabilities": {
                    "type": "chat",
                    "supports": {"reasoning_effort": ["low", "medium", "high", "xhigh"]},
                },
            },
            {
                "id": "claude-haiku-4.5",
                "vendor": "Anthropic",
                "capabilities": {"type": "chat", "supports": {}},
            },
        ]
    }


# --- build_effort_snapshot ---------------------------------------------------


def test_snapshot_present_list_stored_as_set_in_both_id_forms():
    snap = build_effort_snapshot(_models_payload())
    assert snap["claude-opus-4.8"] == {"medium"}
    # Dash form keyed too, so a dash-form request id still resolves.
    assert snap["claude-opus-4-8"] == {"medium"}


def test_snapshot_absent_reasoning_effort_means_strip():
    snap = build_effort_snapshot(_models_payload())
    assert snap["claude-haiku-4.5"] is None
    assert snap["claude-haiku-4-5"] is None


def test_snapshot_empty_list_means_strip():
    payload = {"data": [{"id": "x", "capabilities": {"supports": {"reasoning_effort": []}}}]}
    assert build_effort_snapshot(payload)["x"] is None


def test_snapshot_values_lowercased():
    payload = {"data": [{"id": "x", "capabilities": {"supports": {"reasoning_effort": ["HIGH", "Medium"]}}}]}
    assert build_effort_snapshot(payload)["x"] == {"high", "medium"}


def test_snapshot_malformed_payload_is_empty():
    assert build_effort_snapshot(None) == {}
    assert build_effort_snapshot({"data": "nope"}) == {}
    assert build_effort_snapshot({"data": [42, {"no_id": True}]}) == {}


# --- _resolve_allowed_efforts precedence ------------------------------------


def test_resolve_prefers_live_caps_over_static_table():
    # Static table clamps opus-4.7-1m-internal to default {low,medium,high}
    # (no entry); live caps say xhigh is allowed, which must win.
    caps = {"claude-opus-4.7-1m-internal": {"low", "medium", "high", "xhigh"}}
    assert _resolve_allowed_efforts("claude-opus-4.7-1m-internal", caps) == {
        "low", "medium", "high", "xhigh",
    }


def test_resolve_live_caps_none_means_strip():
    assert _resolve_allowed_efforts("claude-haiku-4.5", {"claude-haiku-4.5": None}) is None


def test_resolve_falls_back_to_static_when_model_absent_from_caps():
    # Model not in the (otherwise populated) snapshot -> static table applies.
    assert _resolve_allowed_efforts("claude-opus-4.8", {"other": {"low"}}) == {"medium"}


def test_resolve_defaults_when_unknown_everywhere():
    assert _resolve_allowed_efforts("claude-sonnet-4.6", None) == {"low", "medium", "high"}


def test_resolve_case_insensitive():
    assert _resolve_allowed_efforts("Claude-Opus-4.8", {"claude-opus-4.8": {"medium"}}) == {"medium"}


# --- _parse_claude_request honoring caps ------------------------------------


def _rt(body: dict, caps=None) -> dict:
    out, _model, _stream = _parse_claude_request(json.dumps(body).encode(), caps=caps)
    return json.loads(out)


def test_parse_caps_allow_xhigh_passes_through():
    caps = {"claude-opus-4.7-1m-internal": {"low", "medium", "high", "xhigh"}}
    out = _rt({"model": "claude-opus-4.7-1m-internal", "reasoning_effort": "xhigh"}, caps=caps)
    assert out["output_config"]["effort"] == "xhigh"


def test_parse_caps_strip_when_unsupported():
    caps = {"claude-sonnet-4.5": None}
    out = _rt({"model": "claude-sonnet-4.5", "output_config": {"effort": "high"}}, caps=caps)
    assert "output_config" not in out


def test_parse_caps_clamp_high_to_medium():
    caps = {"claude-opus-4.8": {"medium"}}
    out = _rt({"model": "claude-opus-4.8", "reasoning_effort": "high"}, caps=caps)
    assert out["output_config"]["effort"] == "medium"


# --- ModelCapabilityCache ----------------------------------------------------


@pytest.mark.asyncio
async def test_cache_fetches_and_caches_single_flight():
    reset_settings_for_tests(_settings())
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/models"
        calls["n"] += 1
        return httpx.Response(200, json=_models_payload())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = ModelCapabilityCache()
    snap1 = await cache.get(client, _settings())
    snap2 = await cache.get(client, _settings())
    assert snap1["claude-opus-4.8"] == {"medium"}
    assert snap2 is snap1
    assert calls["n"] == 1  # second call served from cache
    await client.aclose()
    reset_settings_for_tests(None)


@pytest.mark.asyncio
async def test_cache_fetch_failure_returns_empty_and_does_not_raise():
    reset_settings_for_tests(_settings())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = ModelCapabilityCache()
    snap = await cache.get(client, _settings())
    assert snap == {}  # degrades to "no snapshot" -> caller uses static fallback
    await client.aclose()
    reset_settings_for_tests(None)


@pytest.mark.asyncio
async def test_cache_concurrent_callers_fetch_once():
    """Two callers racing on a cold cache: the first holds the lock through the
    fetch while the second blocks, then the second hits the post-lock
    double-check and returns the freshly-cached snapshot without re-fetching."""
    reset_settings_for_tests(_settings())
    cache = ModelCapabilityCache()
    started = asyncio.Event()
    release = asyncio.Event()
    fetches = {"n": 0}

    async def slow_fetch(_client, _settings, _timeout):
        fetches["n"] += 1
        started.set()
        await release.wait()
        return {"claude-opus-4.8": {"medium"}}

    cache._fetch = slow_fetch  # type: ignore[method-assign]
    task_a = asyncio.create_task(cache.get(None, _settings()))
    await started.wait()  # task_a now holds the lock inside _fetch
    task_b = asyncio.create_task(cache.get(None, _settings()))
    await asyncio.sleep(0)  # let task_b reach and block on the lock
    release.set()
    snap_a, snap_b = await asyncio.gather(task_a, task_b)
    assert snap_a is snap_b
    assert snap_a["claude-opus-4.8"] == {"medium"}
    assert fetches["n"] == 1  # task_b served from cache, no second fetch
    reset_settings_for_tests(None)


@pytest.mark.asyncio
async def test_cache_serves_stale_snapshot_immediately_and_refreshes_in_background():
    """Once a snapshot exists, a stale `get` returns the old value WITHOUT
    blocking and kicks off a single background refresh that updates the cache."""
    reset_settings_for_tests(_settings())
    versions = [
        {"data": [{"id": "m", "capabilities": {"supports": {"reasoning_effort": ["low"]}}}]},
        {"data": [{"id": "m", "capabilities": {"supports": {"reasoning_effort": ["high"]}}}]},
    ]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = versions[min(calls["n"], len(versions) - 1)]
        calls["n"] += 1
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = ModelCapabilityCache(ttl_secs=0.0)  # always considered stale
    first = await cache.get(client, _settings())
    assert first["m"] == {"low"}
    # Stale get returns the old snapshot synchronously, schedules a refresh.
    stale = await cache.get(client, _settings())
    assert stale["m"] == {"low"}
    assert cache._refresh_task is not None
    await cache._refresh_task  # let the background refresh complete
    assert cache._snapshot["m"] == {"high"}
    assert calls["n"] == 2
    await client.aclose()
    reset_settings_for_tests(None)


@pytest.mark.asyncio
async def test_cache_background_refresh_failure_retains_stale_snapshot():
    reset_settings_for_tests(_settings())
    state = {"fail": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(500, json={})
        return httpx.Response(200, json=_models_payload())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = ModelCapabilityCache(ttl_secs=0.0)  # always considered stale
    good = await cache.get(client, _settings())
    assert good["claude-opus-4.8"] == {"medium"}
    state["fail"] = True
    stale = await cache.get(client, _settings())
    assert stale["claude-opus-4.8"] == {"medium"}  # last good returned immediately
    if cache._refresh_task is not None:
        await cache._refresh_task  # background refresh fails, snapshot unchanged
    assert cache._snapshot["claude-opus-4.8"] == {"medium"}
    await client.aclose()
    reset_settings_for_tests(None)


@pytest.mark.asyncio
async def test_cache_does_not_spawn_duplicate_background_refresh():
    reset_settings_for_tests(_settings())
    cache = ModelCapabilityCache(ttl_secs=0.0)
    # Seed a snapshot so subsequent stale gets take the background path.
    cache._snapshot = {"m": {"low"}}
    cache._have_snapshot = True
    cache._expires_at = 0.0
    started = asyncio.Event()
    release = asyncio.Event()
    fetches = {"n": 0}

    async def slow_fetch(_client, _settings, _timeout):
        fetches["n"] += 1
        started.set()
        await release.wait()
        return {"m": {"high"}}

    cache._fetch = slow_fetch  # type: ignore[method-assign]
    snap1 = await cache.get(None, _settings())  # schedules refresh #1
    await started.wait()
    snap2 = await cache.get(None, _settings())  # refresh in flight -> no new task
    assert snap1["m"] == {"low"} and snap2["m"] == {"low"}
    release.set()
    await cache._refresh_task
    assert fetches["n"] == 1  # only one background fetch ran
    reset_settings_for_tests(None)


@pytest.mark.asyncio
async def test_cache_aclose_cancels_inflight_background_refresh():
    reset_settings_for_tests(_settings())
    cache = ModelCapabilityCache(ttl_secs=0.0)
    cache._snapshot = {"m": {"low"}}
    cache._have_snapshot = True
    cache._expires_at = 0.0
    started = asyncio.Event()

    async def hanging_fetch(_client, _settings, _timeout):
        started.set()
        await asyncio.Event().wait()  # never completes
        return {}

    cache._fetch = hanging_fetch  # type: ignore[method-assign]
    await cache.get(None, _settings())  # schedules the hanging background refresh
    await started.wait()
    assert cache._refresh_task is not None and not cache._refresh_task.done()
    await cache.aclose()  # must cancel it cleanly without raising
    assert cache._refresh_task.cancelled()
    await cache.aclose()  # no-op when nothing is in flight
    reset_settings_for_tests(None)


@pytest.mark.asyncio
async def test_cache_aclose_swallows_background_task_exception():
    cache = ModelCapabilityCache()

    async def bad():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise ValueError("boom") from None

    cache._refresh_task = asyncio.create_task(bad())
    await asyncio.sleep(0)  # let it start and block
    await cache.aclose()  # cancel triggers ValueError, which must be swallowed
    assert cache._refresh_task.done()
