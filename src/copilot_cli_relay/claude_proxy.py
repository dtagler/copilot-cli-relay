"""Claude/Anthropic route handlers for /claude/v1 client routes."""
from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Mapping
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from .config import get_settings
from .errors import anthropic_json_error, anthropic_sse_error_event
from .headers import build_claude_outbound_headers
from .logging_setup import logger, redact_bytes, redact_text
from .model_capabilities import EffortCaps
from .proxy_shared import (
    MAX_UPSTREAM_ERROR_BYTES,
    UPSTREAM_TIMEOUT,
    chunks_with_keepalive,
    filter_response_headers,
    passthrough_response,
    read_bounded,
)


def _parse_claude_request(body: bytes, caps: EffortCaps | None = None) -> tuple[bytes, str | None, bool]:
    """Parse the request body once and return (rewritten_body, model, streaming).

    `caps` is an optional upstream capability snapshot (model id -> allowed
    reasoning_effort set, or None to strip). When provided it takes precedence
    over the static `_EFFORT_OVERRIDES` fallback; when None, the static table is
    used (which is what the unit tests and any offline path rely on).

    Falls back to (body, None, False) on malformed input — we still forward to
    upstream so the client gets the upstream's actual error message rather than
    a guess from us. Returns the original body bytes unchanged when no rewrite
    is needed (preserves byte-for-byte shape for any future hash/audit use and
    avoids ensure_ascii expansion of non-ASCII content).
    """
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return body, None, False
    if not isinstance(parsed, dict):
        return body, None, False
    thinking_mutated = _apply_thinking_rewrite(parsed)
    new_body, effort_mutated = _apply_effort_rewrite(parsed, caps)
    mutated = thinking_mutated or effort_mutated
    # Strict boolean: per Anthropic spec `stream` is a JSON boolean. Accepting
    # truthy non-bool values (e.g. "false", 1, {}) here would silently route
    # the request through the streaming path and return the wrong content
    # framing to the client.
    streaming = parsed.get("stream") is True
    return (new_body if mutated else body), parsed.get("model"), streaming


def _anthropic_kind_for_status(status: int) -> str:
    if status == 401:
        return "authentication_error"
    if status == 403:
        return "permission_error"
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if 500 <= status < 600:
        return "api_error"
    return "invalid_request_error"

# Static FALLBACK for per-model reasoning-effort constraints, used only when the
# live `/models` capability snapshot is unavailable (e.g. a /models outage on a
# cold start). The authoritative source is upstream's
# `capabilities.supports.reasoning_effort`, consulted via the model-capability
# cache (see model_capabilities.py). You should NOT need to edit this table when
# a new model appears — it exists so an offline proxy still degrades sensibly.
# Models NOT listed accept the standard {low, medium, high}.
# Keys cover both dot and dash forms — Claude Code may send either; Copilot accepts both.
_EFFORT_OVERRIDES: dict[str, set[str] | None] = {
    # Opus 4.8 and 4.7 currently only accept "medium" (verified upstream via
    # /models capabilities.supports.reasoning_effort == ["medium"]).
    "claude-opus-4.8": {"medium"},
    "claude-opus-4-8": {"medium"},
    "claude-opus-4.7": {"medium"},
    "claude-opus-4-7": {"medium"},
    # Haiku doesn't support reasoning effort at all — strip the field.
    "claude-haiku-4.5": None,
    "claude-haiku-4-5": None,
}
_DEFAULT_EFFORT_VALUES = {"low", "medium", "high"}


# Standard model id → upstream's -1m variant id, used when the client signals
# 1M-context intent via the `context-1m-2025-08-07` anthropic-beta header.
# Upstream advertises only Opus 4.6 and 4.7 in -1m form (verified via
# /models capabilities.limits.max_context_window_tokens=1_000_000); other
# Anthropic models cap at 200K and have no -1m variant on this tenant.
# Keys cover both dot and dash inbound forms — Claude Code may send either.
# Values use DASH form (what Claude Code's `/model` validation accepts);
# the dash→dot conversion required by Copilot's /v1/messages happens later
# in `_normalize_model_for_upstream`.
_MODEL_1M_REWRITES: dict[str, str] = {
    "claude-opus-4.7": "claude-opus-4-7-1m-internal",
    "claude-opus-4-7": "claude-opus-4-7-1m-internal",
    "claude-opus-4.6": "claude-opus-4-6-1m",
    "claude-opus-4-6": "claude-opus-4-6-1m",
}
_BETA_1M_TOKEN = "context-1m-2025-08-07"


def _normalize_effort(value: Any, allowed: set[str]) -> str:
    """Map an arbitrary value into one of the allowed efforts."""
    if not isinstance(value, str):
        return "medium" if "medium" in allowed else next(iter(allowed))
    v = value.strip().lower()
    # If the value is already directly accepted (e.g. a model whose live caps
    # advertise "xhigh"), keep it as-is before applying the downgrade aliases.
    if v in allowed:
        return v
    # Common non-standard variants Claude Code may send, mapped to the nearest
    # standard tier for models that don't accept the exotic value.
    aliases = {
        "xhigh": "high", "x-high": "high", "extra-high": "high", "extreme": "high",
        "xlow": "low", "x-low": "low", "minimal": "low", "none": "low",
    }
    v = aliases.get(v, v)
    if v in allowed:
        return v
    # Fall back to medium if available, else any allowed value.
    if "medium" in allowed:
        return "medium"
    return next(iter(allowed))


def _resolve_allowed_efforts(model: Any, caps: EffortCaps | None) -> set[str] | None:
    """Resolve the allowed reasoning-effort set for `model`.

    Precedence: live upstream snapshot (`caps`) > static `_EFFORT_OVERRIDES`
    fallback > default {low, medium, high}. A returned value of None means
    "strip the effort field" (model advertises no reasoning_effort support).
    Lookups are case-insensitive and match either dot or dash id form because
    the snapshot stores both and the static table is keyed in both forms.
    """
    if isinstance(model, str):
        key = model.lower()
        if caps is not None and key in caps:
            return caps[key]
        if key in _EFFORT_OVERRIDES:
            return _EFFORT_OVERRIDES[key]
    return _DEFAULT_EFFORT_VALUES


def _apply_thinking_rewrite(parsed: dict) -> bool:
    """Normalize the `thinking` block for Copilot's Anthropic endpoint.

    Copilot no longer supports `thinking.type: "enabled"` ("Use
    thinking.type.adaptive and output_config.effort") and rejects
    `budget_tokens` under the adaptive shape ("Extra inputs are not
    permitted"). So we convert `enabled` -> `adaptive` and strip `budget_tokens`
    from either shape; thinking depth is governed by output_config.effort now.
    Other thinking types (e.g. "disabled") pass through untouched. Returns True
    if the body was changed.
    """
    thinking = parsed.get("thinking")
    if not isinstance(thinking, dict):
        return False
    ttype = thinking.get("type")
    if ttype not in ("enabled", "adaptive"):
        return False
    mutated = False
    if ttype == "enabled":
        thinking["type"] = "adaptive"
        mutated = True
    if "budget_tokens" in thinking:
        thinking.pop("budget_tokens", None)
        mutated = True
    if mutated:
        logger.debug("normalized thinking block for model=%s", parsed.get("model"))
    return mutated


def _apply_effort_rewrite(parsed: dict, caps: EffortCaps | None = None) -> tuple[bytes, bool]:
    """Mutate `parsed` in place to normalize reasoning-effort, then serialize.

    Copilot's Anthropic endpoint controls reasoning effort via
    `output_config.effort` and no longer accepts a top-level `reasoning_effort`
    field ("Extra inputs are not permitted"). So we (1) relocate any top-level
    `reasoning_effort` into `output_config.effort`, then (2) clamp that value to
    the model's accepted set, rewriting to the closest accepted value rather
    than letting the request 400. The accepted set is resolved from the live
    capability snapshot when available (see `_resolve_allowed_efforts`),
    otherwise the static fallback. Returns (serialized, mutated). When `mutated`
    is False, callers should prefer the original request bytes so we don't
    reshape the payload unnecessarily.
    """
    model = parsed.get("model")
    allowed = _resolve_allowed_efforts(model, caps)

    mutated = False

    # (1) Relocate a top-level `reasoning_effort` into `output_config.effort`.
    # Copilot rejects the top-level field outright now; an existing
    # `output_config.effort` is the canonical control and wins if both are set.
    if "reasoning_effort" in parsed:
        moved = parsed.pop("reasoning_effort")
        mutated = True
        if isinstance(moved, str):
            oc = parsed.get("output_config")
            if not isinstance(oc, dict):
                oc = {}
                parsed["output_config"] = oc
            oc.setdefault("effort", moved)
        logger.debug("relocated reasoning_effort -> output_config.effort for model=%s", model)

    # (2) Clamp output_config.effort against the model's accepted set.
    oc = parsed.get("output_config")
    if isinstance(oc, dict) and "effort" in oc:
        if allowed is None:
            # Model doesn't support reasoning effort at all — strip the field
            # regardless of value type, so any shape we'd send is wrong.
            oc.pop("effort", None)
            mutated = True
            logger.debug("stripped output_config.effort for model=%s (not supported)", model)
        else:
            original = oc["effort"]
            if isinstance(original, str):
                new = _normalize_effort(original, allowed)
                if new != original:
                    oc["effort"] = new
                    mutated = True
                    logger.debug(
                        "clamped output_config.effort for model=%s: %r -> %r (allowed=%s)",
                        model, original, new, sorted(allowed),
                    )
            # Non-string values pass through (future API expansion) — let
            # upstream return its own error rather than silently coercing.

    # Drop a now-empty output_config so we don't forward `output_config: {}`.
    oc = parsed.get("output_config")
    if isinstance(oc, dict) and not oc:
        parsed.pop("output_config", None)
        mutated = True

    # ensure_ascii=False so unicode (emoji, identifiers) isn't \uXXXX-escaped,
    # which both bloats the payload and makes upstream-side debugging harder.
    return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), mutated


def _client_wants_1m_context(headers: Mapping[str, str]) -> bool:
    """True if the inbound request carries the Anthropic 1M-context beta token.

    Claude Code's hardcoded 'Sonnet (1M context)' / 'Opus 4.7 (1M context)'
    picker tiers attach `anthropic-beta: context-1m-2025-08-07` and send the
    standard model id. Copilot rejects that beta on standard ids and we strip
    it in headers.py — so without an extra hop, those picker tiers silently
    downgrade to 200K context. Detecting the intent here lets `_remap_to_1m`
    swap the model id to upstream's actual 1M-capable variant.
    """
    for k, v in headers.items():
        if k.lower() != "anthropic-beta":
            continue
        if any(t.strip().lower() == _BETA_1M_TOKEN for t in v.split(",")):
            return True
    return False


def _swap_model_in_body(body: bytes, new_model: str) -> tuple[bytes, str | None]:
    """Re-serialize the request body with the `model` field replaced.

    Returns (new_body, new_model). On any structural problem (malformed JSON,
    non-dict root) returns (original body, None) so the caller can no-op.
    """
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return body, None
    if not isinstance(parsed, dict):
        return body, None
    parsed["model"] = new_model
    new_body = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return new_body, new_model


def _remap_to_1m(body: bytes, model_id: str | None) -> tuple[bytes, str | None, bool]:
    """If `model_id` has a known -1m variant, rewrite the body to use it.

    Returns (new_body, new_model_id, mutated). When the model isn't in the
    rewrite table — e.g. Sonnet variants, where upstream has no -1m form on
    this tenant — returns the original body unchanged and the call is a no-op.
    The output id is in DASH form (what Claude Code's `/model` accepts);
    `_normalize_model_for_upstream` later converts it to the dot form
    Copilot's /v1/messages requires.
    """
    if not isinstance(model_id, str):
        return body, model_id, False
    target = _MODEL_1M_REWRITES.get(model_id.lower())
    if not target:
        return body, model_id, False
    new_body, new_model = _swap_model_in_body(body, target)
    if new_model is None:
        return body, model_id, False
    return new_body, new_model, True


def _normalize_model_for_upstream(body: bytes, model_id: str | None) -> tuple[bytes, str | None, bool]:
    """Apply the dash→dot conversion required for any -1m / -1m-internal id.

    `/claude/v1/models` advertises ids in canonical Anthropic dash form because
    Claude Code's `/model` slash command validates against that form. But
    Copilot's `/v1/messages` rejects the dash form for `-1m` ids
    ("model_not_supported") and only accepts the dot form. We do this as
    the last hop before forwarding so the user-visible id always stays dash.

    Generic conversion: turn the version-segment hyphens (e.g. `-4-7-`) into
    a dot (`-4.7-`) for any id matching `*-N-N-1m*`. Future upstream additions
    like `claude-sonnet-4-6-1m` will be handled without table updates. Other
    models pass through unchanged (upstream accepts dash and dot for them).
    """
    if not isinstance(model_id, str):
        return body, model_id, False
    target = _to_upstream_dot_form(model_id)
    if target is None or target == model_id:
        return body, model_id, False
    new_body, new_model = _swap_model_in_body(body, target)
    if new_model is None:
        return body, model_id, False
    return new_body, new_model, True


# Match an Anthropic id of the form `claude-<family>-<major>-<minor>-1m...`
# in dash form. Group 1 is the prefix up to (and including) the version-segment
# hyphen, group 2 is the major.minor pair as `N-N`, group 3 is the suffix
# starting at the `-1m` marker.
_DASH_1M_VERSION_RE = re.compile(r"^(claude-[a-z]+-)(\d+)-(\d+)(-1m\b.*)$", re.IGNORECASE)


def _to_upstream_dot_form(model_id: str) -> str | None:
    """If `model_id` is a dash-form -1m id, return the dot-form Copilot needs.

    Returns None if the id isn't a recognized -1m shape; callers should treat
    that as "leave alone". Returns `model_id` unchanged if the version segment
    is already dot form (e.g. user typed `claude-opus-4.7-1m-internal`).
    """
    m = _DASH_1M_VERSION_RE.match(model_id)
    if not m:
        return None
    return f"{m.group(1)}{m.group(2)}.{m.group(3)}{m.group(4)}"


async def _get_caps_snapshot(request: Request) -> EffortCaps | None:
    """Best-effort fetch of the upstream effort-capability snapshot.

    Returns None when no cache is attached (e.g. tests that don't set
    `app.state.model_caps`), so the request falls back to the static table.
    `ModelCapabilityCache.get` never raises, so a /models outage can't break
    the in-flight messages request.
    """
    cache = getattr(request.app.state, "model_caps", None)
    if cache is None:
        return None
    client: httpx.AsyncClient = request.app.state.http_client
    return await cache.get(client, get_settings())


def _peek_model(body: bytes) -> str | None:
    """Read the `model` field without mutating or re-serializing the body.

    Used to drive the 1M remap before reasoning-effort clamping; returns None
    on malformed JSON or a non-dict root so callers no-op.
    """
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return None
    return parsed.get("model") if isinstance(parsed, dict) else None


async def proxy_claude_messages(request: Request) -> Response:
    settings = get_settings()
    client: httpx.AsyncClient = request.app.state.http_client

    raw_body = await request.body()
    caps = await _get_caps_snapshot(request)
    # Finalize the upstream model id (1M beta remap, then dash->dot) BEFORE
    # clamping reasoning-effort, so the clamp keys off the model actually being
    # invoked. Claude Code's 1M picker tier sends e.g. `claude-opus-4-7` + the
    # context-1m beta header but the request really runs on
    # `claude-opus-4.7-1m-internal`, which advertises a wider effort set — so
    # clamping against the inbound id would wrongly downgrade. The remap and
    # normalize steps only ever rewrite the `model` field, never effort.
    body = raw_body
    model_id = _peek_model(raw_body)
    if _client_wants_1m_context(request.headers):
        body, new_model_id, remapped = _remap_to_1m(body, model_id)
        if remapped:
            logger.debug(
                "1M context remap: %s -> %s (anthropic-beta %s)",
                model_id, new_model_id, _BETA_1M_TOKEN,
            )
            model_id = new_model_id
    # Last hop before upstream: convert -1m ids from the dash form Claude Code
    # uses (and we advertise in /claude/v1/models) to the dot form Copilot requires.
    body, model_id, _ = _normalize_model_for_upstream(body, model_id)
    # Clamp reasoning-effort last, keyed on the finalized model id.
    body, model_id, streaming = _parse_claude_request(body, caps=caps)
    request_id = str(uuid.uuid4())
    model = model_id or "?"
    started = time.monotonic()

    headers = build_claude_outbound_headers(
        request.headers,
        bearer_token=settings.github_token,
        integration_id=settings.integration_id,
        editor_version=settings.editor_version,
        request_id=request_id,
    )
    if settings.log_bodies:
        logger.debug(
            "Claude POST /claude/v1/messages -> upstream /v1/messages model=%s body=%s",
            model,
            redact_bytes(body).decode("utf-8", "replace"),
        )

    upstream_url = f"{settings.api_base}/v1/messages"

    if not streaming:
        # Use stream=True so we can peek the status before the entire body is
        # buffered. For 2xx we then read the full body for passthrough; for
        # >=400 we cap the read at MAX_UPSTREAM_ERROR_BYTES so a hostile or
        # misconfigured upstream can't push us into unbounded memory use.
        req = client.build_request(
            "POST", upstream_url, content=body, headers=headers, timeout=UPSTREAM_TIMEOUT
        )
        try:
            resp = await client.send(req, stream=True)
        except httpx.TimeoutException as exc:
            logger.warning("upstream timeout request_id=%s err=%s", request_id, redact_text(str(exc)))
            return anthropic_json_error("api_error", f"Upstream timeout: {redact_text(str(exc))}")
        except httpx.HTTPError as exc:
            logger.warning("upstream error request_id=%s err=%s", request_id, redact_text(str(exc)))
            return anthropic_json_error("api_error", f"Upstream error: {redact_text(str(exc))}")

        try:
            if resp.status_code >= 400:
                upstream_status = resp.status_code
                upstream_headers = resp.headers
                try:
                    err_bytes = await read_bounded(resp, MAX_UPSTREAM_ERROR_BYTES)
                except Exception as exc:
                    logger.warning(
                        "upstream %d on non-stream request_id=%s; failed to read error body: %s",
                        upstream_status, request_id, redact_text(str(exc)),
                    )
                    err_bytes = b""
                duration_ms = int((time.monotonic() - started) * 1000)
                logger.info(
                    "Claude POST /claude/v1/messages -> upstream /v1/messages model=%s status=%d duration_ms=%d request_id=%s stream=0",
                    model, upstream_status, duration_ms, request_id,
                )
                return _build_non_streaming_error(
                    upstream_status, upstream_headers, err_bytes, request_id
                )
            # Success: read the full body for passthrough.
            try:
                await resp.aread()
            except httpx.TimeoutException as exc:
                safe = redact_text(str(exc))
                logger.warning("upstream read timeout request_id=%s err=%s", request_id, safe)
                return anthropic_json_error("api_error", f"Upstream timeout: {safe}")
            except httpx.HTTPError as exc:
                safe = redact_text(str(exc))
                logger.warning("upstream read error request_id=%s err=%s", request_id, safe)
                return anthropic_json_error("api_error", f"Upstream error: {safe}")
            except Exception as exc:
                safe = redact_text(str(exc))
                logger.warning("upstream read error request_id=%s err=%s", request_id, safe)
                return anthropic_json_error("api_error", f"Upstream error: {safe}")
        finally:
            await resp.aclose()

        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "Claude POST /claude/v1/messages -> upstream /v1/messages model=%s status=%d duration_ms=%d request_id=%s stream=0",
            model, resp.status_code, duration_ms, request_id,
        )
        return passthrough_response(resp)

    return await _stream_response(
        client=client, url=upstream_url, body=body, headers=headers,
        request=request, model=model, request_id=request_id, started=started,
    )


def _build_non_streaming_error(
    status: int,
    upstream_headers: httpx.Headers,
    err_bytes: bytes,
    request_id: str,
) -> Response:
    """Wrap a bounded upstream error body in an Anthropic JSON envelope.

    `err_bytes` is already capped at MAX_UPSTREAM_ERROR_BYTES by the caller's
    `read_bounded` so this function only handles redaction, header forwarding,
    and envelope construction. Without the bound + redaction, a hostile or
    misconfigured upstream could (a) push the proxy into unbounded memory use
    via a giant error body and (b) reflect injected request headers — the
    proxy's own Bearer token included — verbatim into the local client.
    """
    err_text = err_bytes.decode("utf-8", "replace")
    redacted = redact_text(err_text)
    settings = get_settings()
    if settings.log_bodies:
        logger.warning(
            "upstream %d on non-stream request_id=%s body=%s",
            status, request_id, redacted[:500],
        )
    else:
        logger.warning(
            "upstream %d on non-stream request_id=%s (body suppressed; set LOG_BODIES=1 to log redacted body)",
            status, request_id,
        )
    forwarded_headers: dict[str, str] = {}
    for k, v in upstream_headers.items():
        lk = k.lower()
        if lk in ("retry-after", "www-authenticate") or lk.startswith("x-ratelimit-"):
            forwarded_headers[k] = v
    msg = (
        f"Upstream {status}: {redacted[:300]}"
        if redacted
        else f"Upstream {status} (error body unavailable)"
    )
    return anthropic_json_error(
        _anthropic_kind_for_status(status),
        msg,
        status=status,
        headers=forwarded_headers or None,
    )


async def _stream_response(
    *,
    client: httpx.AsyncClient,
    url: str,
    body: bytes,
    headers: dict[str, str],
    request: Request,
    model: str,
    request_id: str,
    started: float,
) -> Response:
    """Open the upstream stream, peek the status, and choose the right shape.

    For pre-stream upstream errors (status >= 400 before any body byte was
    forwarded) we return a real HTTP error JSONResponse so SDK retry logic
    that keys on HTTP status (401 → re-auth, 429 → backoff, …) fires
    correctly. Only after the stream has truly started do mid-stream failures
    surface as a terminal `event: error` SSE frame.
    """
    req = client.build_request("POST", url, content=body, headers=headers, timeout=UPSTREAM_TIMEOUT)
    try:
        upstream = await client.send(req, stream=True)
    except httpx.TimeoutException as exc:
        safe = redact_text(str(exc))
        logger.warning("upstream stream timeout request_id=%s err=%s", request_id, safe)
        return anthropic_json_error("api_error", f"Upstream timeout: {safe}")
    except Exception as exc:
        # Catch broadly so non-httpx errors raised by the transport (OSError,
        # ssl.SSLError, etc.) still become a clean Anthropic-shaped envelope
        # rather than a 500 from the Starlette default handler.
        safe = redact_text(str(exc))
        logger.warning("upstream stream connect error request_id=%s err=%s", request_id, safe)
        return anthropic_json_error("api_error", f"Upstream stream error: {safe}")

    if upstream.status_code >= 400:
        upstream_status = upstream.status_code
        # Capture rate-limit / auth-challenge headers BEFORE aclose — SDK
        # retry logic on 429/401 keys on Retry-After / WWW-Authenticate /
        # X-RateLimit-* and we drop them otherwise.
        forwarded_headers: dict[str, str] = {}
        for k, v in upstream.headers.items():
            lk = k.lower()
            if lk in ("retry-after", "www-authenticate") or lk.startswith("x-ratelimit-"):
                forwarded_headers[k] = v
        try:
            try:
                err_bytes = await read_bounded(upstream, MAX_UPSTREAM_ERROR_BYTES)
            except Exception as exc:
                # The error-body stream itself can fail (httpx.ReadError, OSError,
                # etc.). Don't let that escape — we still know the upstream
                # status and want to preserve it in the JSON envelope.
                logger.warning(
                    "upstream %d on stream request_id=%s; failed to read error body: %s",
                    upstream_status, request_id, redact_text(str(exc)),
                )
                err_bytes = b""
        finally:
            await upstream.aclose()
        err_text = err_bytes.decode("utf-8", "replace")
        settings = get_settings()
        redacted = redact_text(err_text)
        if settings.log_bodies:
            logger.warning(
                "upstream %d on stream request_id=%s body=%s",
                upstream_status, request_id, redacted[:500],
            )
        else:
            logger.warning(
                "upstream %d on stream request_id=%s (body suppressed; set LOG_BODIES=1 to log redacted body)",
                upstream_status, request_id,
            )
        msg = (
            f"Upstream {upstream_status}: {redacted[:300]}"
            if redacted
            else f"Upstream {upstream_status} (error body unavailable)"
        )
        return anthropic_json_error(
            _anthropic_kind_for_status(upstream_status),
            msg,
            status=upstream_status,
            headers=forwarded_headers or None,
        )

    ttfb = int((time.monotonic() - started) * 1000)
    logger.info(
        "Claude POST /claude/v1/messages -> upstream /v1/messages model=%s status=%d ttfb_ms=%d request_id=%s stream=1",
        model, upstream.status_code, ttfb, request_id,
    )

    async def body_iter():
        try:
            async for chunk, sentinel in chunks_with_keepalive(upstream, request):
                if sentinel == "disconnect":
                    return
                if sentinel == "ping":
                    yield b"event: ping\ndata: {\"type\":\"ping\"}\n\n"
                    continue
                yield chunk
        except Exception as exc:
            # Catch broadly: the producer in chunks_with_keepalive captures
            # any non-cancellation Exception (OSError, ssl.SSLError,
            # anyio.BrokenResourceError, etc.) and re-raises it here. An
            # uncaught one would silently truncate the SSE stream — exactly
            # the failure mode the keepalive design exists to prevent.
            # asyncio.CancelledError inherits from BaseException, so it still
            # propagates correctly.
            safe = redact_text(str(exc))
            logger.warning("stream error request_id=%s err=%s", request_id, safe)
            yield anthropic_sse_error_event("api_error", f"Upstream stream error: {safe}")
        finally:
            await upstream.aclose()

    # Forward upstream response headers (request-id correlation, vendor
    # rate-limit hints, etc.) on the streaming success path the same way
    # passthrough_response does for non-streaming, less hop-by-hop and our
    # own framing/content-type.
    response_headers = filter_response_headers(
        upstream.headers,
        also_drop={"content-type", "content-length", "content-encoding"},
    )
    response_headers["Cache-Control"] = "no-cache"
    response_headers["X-Accel-Buffering"] = "no"

    return StreamingResponse(
        body_iter(),
        media_type="text/event-stream",
        headers=response_headers,
    )

async def proxy_claude_models(request: Request) -> Response:
    settings = get_settings()
    client: httpx.AsyncClient = request.app.state.http_client
    headers = build_claude_outbound_headers(
        {},
        bearer_token=settings.github_token,
        integration_id=settings.integration_id,
        editor_version=settings.editor_version,
    )
    headers["Accept"] = "application/json"
    try:
        resp = await client.get(f"{settings.api_base}/models", headers=headers, timeout=30.0)
    except httpx.HTTPError as exc:
        return anthropic_json_error("api_error", f"Upstream /models error: {redact_text(str(exc))}")
    if resp.status_code != 200:
        return anthropic_json_error(
            _anthropic_kind_for_status(resp.status_code),
            f"Upstream /models {resp.status_code}: {redact_text(resp.text)[:300]}",
            status=resp.status_code,
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        logger.warning("upstream /models 200 with non-JSON body: %s", exc)
        return anthropic_json_error(
            "api_error",
            f"Upstream /models returned non-JSON body: {redact_text(resp.text)[:200]}",
        )
    raw = payload.get("data", []) if isinstance(payload, dict) else []
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for m in raw:
        if not _is_anthropic(m):
            continue
        # Defensive type checks: a malformed-but-valid-JSON payload from
        # upstream must not reach Starlette's plain-text 500 handler. Each
        # field we read is validated rather than trusted.
        caps = m.get("capabilities")
        if not isinstance(caps, dict) or caps.get("type") != "chat":
            continue
        if not (m.get("model_picker_enabled", True)):
            continue
        mid = m.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        # Hide internal/1m preview variants — Claude Code auto-enables an
        # unsupported beta header (context-1m-2025-08-07) when these are present
        # in the picker, and Copilot rejects it.
        name_raw = m.get("name")
        name = name_raw if isinstance(name_raw, str) else ""
        # Hide internal/preview variants whose name shouts "internal only" but
        # KEEP the -1m / -1m-internal id-suffix variants — those are the only
        # way to actually get 1M context on this Copilot tenant (verified via
        # /models limits.max_context_window_tokens=1_000_000). Hiding them
        # would make `/model claude-opus-4-7-1m-internal` fail at the picker
        # layer and force users back to the silently-downgrading 200K path.
        # Claude Code's auto-attached `context-1m-2025-08-07` beta header is
        # still rejected by Copilot, but headers.py strips it (and proxy_claude_messages
        # remaps the model id when the beta is present, see _remap_to_1m).
        if "internal only" in name.lower() and not (mid.endswith("-1m") or mid.endswith("-1m-internal")):
            continue
        # Normalize id to canonical Anthropic dash form (claude-opus-4.7 →
        # claude-opus-4-7). Claude Code recognizes this format and validates
        # `/model` arguments against it. For -1m / -1m-internal ids upstream
        # actually requires the dot form; we restore that just before
        # forwarding via _normalize_model_for_upstream so the user-facing id
        # stays dash everywhere Claude Code touches it.
        canonical_id = mid.replace(".", "-")
        # Dedup post-canonicalization so that upstream returning both dot and
        # dash forms of the same model doesn't yield duplicate ids downstream.
        if canonical_id in seen_ids:
            continue
        seen_ids.add(canonical_id)
        items.append({
            "type": "model",
            "id": canonical_id,
            "display_name": name or canonical_id,
            "created_at": "2024-01-01T00:00:00Z",
        })
    body_out = {
        "data": items,
        "has_more": False,
        "first_id": items[0]["id"] if items else None,
        "last_id": items[-1]["id"] if items else None,
    }
    return JSONResponse(body_out)


def _is_anthropic(model: object) -> bool:
    return isinstance(model, dict) and (model.get("vendor") or "").lower() == "anthropic"


async def claude_healthz(request: Request) -> Response:
    settings = get_settings()
    client: httpx.AsyncClient = request.app.state.http_client
    model_count = 0
    upstream_ok = False
    upstream_status: int | None = None
    hint: str | None = None
    try:
        resp = await client.get(
            f"{settings.api_base}/models",
            headers=build_claude_outbound_headers(
                {},
                bearer_token=settings.github_token,
                integration_id=settings.integration_id,
                editor_version=settings.editor_version,
            ),
            timeout=10.0,
        )
        upstream_status = resp.status_code
        if resp.status_code == 200:
            try:
                payload = resp.json()
            except ValueError as exc:
                logger.warning("claude_healthz upstream 200 with non-JSON body: %s", exc)
            else:
                upstream_ok = True
                data = payload.get("data", []) if isinstance(payload, dict) else []
                model_count = sum(1 for m in data if _is_anthropic(m))
        elif resp.status_code in (401, 403):
            hint = "token may be expired; re-run scripts/extract-token.ps1 then docker compose restart proxy"
    except Exception as exc:
        logger.warning("claude_healthz upstream check failed: %s", redact_text(str(exc)))
    body = {
        "ok": True,
        "upstream_ok": upstream_ok,
        "upstream_status": upstream_status,
        "anthropic_models": model_count,
    }
    if hint:
        body["hint"] = hint
    return JSONResponse(body)
