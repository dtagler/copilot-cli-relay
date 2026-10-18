"""OpenAI Responses-compatible proxy routes for Codex CLI and Desktop."""
from __future__ import annotations

import json
import math
import time
import uuid
from contextlib import aclosing
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import get_settings
from .errors import openai_json_error, openai_sse_error_event
from .headers import build_codex_outbound_headers
from .logging_setup import logger, redact_bytes, redact_text
from .proxy_shared import (
    MAX_UPSTREAM_ERROR_BYTES,
    UPSTREAM_TIMEOUT,
    UpstreamStreamingResponse,
    chunks_with_keepalive,
    filter_response_headers,
    passthrough_response,
    read_bounded,
)

COMPACTION_PROMPT = """You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly continue the work."""

_DEFAULT_CODEX_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
_REASONING_EFFORT_DESCRIPTIONS = {
    "none": "Disables reasoning",
    "minimal": "Minimal reasoning",
    "low": "Fast responses with lighter reasoning",
    "medium": "Balances speed and reasoning depth",
    "high": "Greater reasoning depth",
    "xhigh": "Extra high reasoning depth",
    "max": "Maximum reasoning depth",
    "ultra": "Ultra reasoning depth",
}


def _openai_kind_for_status(status: int) -> str:
    if status == 401:
        return "authentication_error"
    if status == 403:
        return "permission_error"
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if 500 <= status < 600:
        return "server_error"
    return "invalid_request_error"


def _is_agent_call(parsed: dict[str, Any]) -> bool:
    input_value = parsed.get("input")
    if not isinstance(input_value, list):
        return False
    for item in input_value:
        if not isinstance(item, dict):
            continue
        if item.get("role") == "assistant":
            return True
        if item.get("type") in {
            "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output",
        }:
            return True
    return False


def _choice_targets_stripped_tool(choice: object, stripped: set[str]) -> bool:
    if isinstance(choice, str):
        return choice in stripped
    if not isinstance(choice, dict):
        return False
    if choice.get("type") in stripped or choice.get("name") in stripped:
        return True
    function = choice.get("function")
    return isinstance(function, dict) and function.get("name") in stripped


def _rewrite_codex_body(parsed: dict[str, Any], *, request_id: str) -> bool:
    mutated = False
    stripped_tools: set[str] = set()

    if "previous_response_id" in parsed:
        parsed.pop("previous_response_id", None)
        mutated = True
        logger.warning("stripped previous_response_id for Codex request_id=%s", request_id)

    tools = parsed.get("tools")
    if isinstance(tools, list):
        kept_tools = []
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") == "image_generation":
                stripped_tools.add("image_generation")
                continue
            kept_tools.append(tool)
        if len(kept_tools) != len(tools):
            mutated = True
            logger.warning(
                "stripped unsupported Codex tools request_id=%s tools=%s",
                request_id,
                ",".join(sorted(stripped_tools)),
            )
            if kept_tools:
                parsed["tools"] = kept_tools
            else:
                parsed.pop("tools", None)
        if stripped_tools and (
            _choice_targets_stripped_tool(parsed.get("tool_choice"), stripped_tools)
            or (not kept_tools and parsed.get("tool_choice") == "required")
        ):
            parsed.pop("tool_choice", None)
            mutated = True
    return mutated


def _serialize_json_body(parsed: dict[str, Any]) -> bytes:
    return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _parse_codex_request(body: bytes, *, request_id: str) -> tuple[bytes, str | None, bool, str]:
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return body, None, False, "agent"
    if not isinstance(parsed, dict):
        return body, None, False, "agent"

    mutated = _rewrite_codex_body(parsed, request_id=request_id)
    streaming = parsed.get("stream") is True
    model = parsed.get("model") if isinstance(parsed.get("model"), str) else None
    initiator = "agent" if _is_agent_call(parsed) else "user"
    return (_serialize_json_body(parsed) if mutated else body), model, streaming, initiator


def _build_codex_headers(
    request: Request,
    *,
    request_id: str,
    initiator: str,
    streaming: bool = False,
) -> dict[str, str]:
    settings = get_settings()
    return build_codex_outbound_headers(
        request.headers,
        bearer_token=settings.github_token,
        integration_id=settings.codex_integration_id,
        editor_version=settings.codex_editor_version,
        plugin_version=settings.codex_plugin_version,
        user_agent=settings.codex_user_agent,
        github_api_version=settings.codex_github_api_version,
        session_id=settings.codex_session_id,
        machine_id=settings.codex_machine_id,
        request_id=request_id,
        initiator=initiator,
        accept="text/event-stream" if streaming else "application/json",
    )


def _forwardable_error_headers(upstream_headers: httpx.Headers) -> dict[str, str] | None:
    forwarded: dict[str, str] = {}
    for k, v in upstream_headers.items():
        lk = k.lower()
        if lk in ("retry-after", "www-authenticate") or lk.startswith("x-ratelimit-"):
            forwarded_headers_name = k
            forwarded[forwarded_headers_name] = v
    return forwarded or None


def _build_openai_upstream_error(
    status: int,
    upstream_headers: httpx.Headers,
    err_bytes: bytes,
    request_id: str,
    route: str,
) -> Response:
    redacted = redact_bytes(err_bytes, max_bytes=MAX_UPSTREAM_ERROR_BYTES).decode("utf-8", "replace")
    settings = get_settings()
    if settings.log_bodies:
        logger.warning("upstream %d on %s request_id=%s body=%s", status, route, request_id, redacted[:500])
    else:
        logger.warning(
            "upstream %d on %s request_id=%s (body suppressed; set LOG_BODIES=1 to log redacted body)",
            status,
            route,
            request_id,
        )
    msg = (
        f"Upstream {status}: {redacted[:300]}"
        if redacted
        else f"Upstream {status} (error body unavailable)"
    )
    return openai_json_error(
        _openai_kind_for_status(status),
        msg,
        status=status,
        headers=_forwardable_error_headers(upstream_headers),
    )


async def _send_non_streaming(
    *,
    client: httpx.AsyncClient,
    url: str,
    body: bytes,
    headers: dict[str, str],
    request_id: str,
    route: str,
) -> Response:
    req = client.build_request("POST", url, content=body, headers=headers, timeout=UPSTREAM_TIMEOUT)
    try:
        resp = await client.send(req, stream=True)
    except httpx.TimeoutException as exc:
        safe = redact_text(str(exc))
        logger.warning("upstream Codex timeout request_id=%s err=%s", request_id, safe)
        return openai_json_error("server_error", f"Upstream timeout: {safe}")
    except httpx.HTTPError as exc:
        safe = redact_text(str(exc))
        logger.warning("upstream Codex error request_id=%s err=%s", request_id, safe)
        return openai_json_error("server_error", f"Upstream error: {safe}")

    try:
        if resp.status_code >= 400:
            try:
                err_bytes = await read_bounded(resp, MAX_UPSTREAM_ERROR_BYTES)
            except Exception as exc:
                logger.warning(
                    "upstream %d on %s request_id=%s; failed to read error body: %s",
                    resp.status_code,
                    route,
                    request_id,
                    redact_text(str(exc)),
                )
                err_bytes = b""
            return _build_openai_upstream_error(resp.status_code, resp.headers, err_bytes, request_id, route)
        try:
            await resp.aread()
        except httpx.TimeoutException as exc:
            safe = redact_text(str(exc))
            logger.warning("upstream Codex timeout request_id=%s err=%s", request_id, safe)
            return openai_json_error("server_error", f"Upstream timeout: {safe}")
        except httpx.HTTPError as exc:
            safe = redact_text(str(exc))
            logger.warning("upstream Codex error request_id=%s err=%s", request_id, safe)
            return openai_json_error("server_error", f"Upstream error: {safe}")
        except Exception as exc:
            safe = redact_text(str(exc))
            logger.warning("upstream Codex error request_id=%s err=%s", request_id, safe)
            return openai_json_error("server_error", f"Upstream error: {safe}")
    finally:
        await resp.aclose()
    return passthrough_response(resp)


async def _codex_stream_response(
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
    req = client.build_request("POST", url, content=body, headers=headers, timeout=UPSTREAM_TIMEOUT)
    try:
        upstream = await client.send(req, stream=True)
    except httpx.TimeoutException as exc:
        safe = redact_text(str(exc))
        logger.warning("upstream Codex stream timeout request_id=%s err=%s", request_id, safe)
        return openai_json_error("server_error", f"Upstream timeout: {safe}")
    except Exception as exc:
        safe = redact_text(str(exc))
        logger.warning("upstream Codex stream connect error request_id=%s err=%s", request_id, safe)
        return openai_json_error("server_error", f"Upstream stream error: {safe}")

    if upstream.status_code >= 400:
        upstream_status = upstream.status_code
        try:
            try:
                err_bytes = await read_bounded(upstream, MAX_UPSTREAM_ERROR_BYTES)
            except Exception as exc:
                logger.warning(
                    "upstream %d on Codex stream request_id=%s; failed to read error body: %s",
                    upstream_status,
                    request_id,
                    redact_text(str(exc)),
                )
                err_bytes = b""
        finally:
            await upstream.aclose()
        return _build_openai_upstream_error(
            upstream_status,
            upstream.headers,
            err_bytes,
            request_id,
            "Codex stream",
        )

    ttfb = int((time.monotonic() - started) * 1000)
    logger.info(
        "POST /codex/v1/responses model=%s status=%d ttfb_ms=%d request_id=%s stream=1",
        model,
        upstream.status_code,
        ttfb,
        request_id,
    )

    async def body_iter():
        try:
            async with aclosing(chunks_with_keepalive(upstream, request)) as chunks:
                async for chunk, sentinel in chunks:
                    if sentinel == "disconnect":
                        return
                    if sentinel == "ping":
                        yield b": keepalive\n\n"
                        continue
                    yield chunk
        except Exception as exc:
            safe = redact_text(str(exc))
            logger.warning("Codex stream error request_id=%s err=%s", request_id, safe)
            yield openai_sse_error_event("server_error", f"Upstream stream error: {safe}")
        finally:
            await upstream.aclose()

    response_headers = filter_response_headers(
        upstream.headers,
        also_drop={"content-type", "content-length", "content-encoding"},
    )
    response_headers["Cache-Control"] = "no-cache"
    response_headers["X-Accel-Buffering"] = "no"
    return UpstreamStreamingResponse(body_iter(), upstream=upstream, headers=response_headers)


async def proxy_codex_responses(request: Request) -> Response:
    settings = get_settings()
    client: httpx.AsyncClient = request.app.state.http_client
    request_id = str(uuid.uuid4())
    started = time.monotonic()

    raw_body = await request.body()
    body, model_id, streaming, initiator = _parse_codex_request(raw_body, request_id=request_id)
    model = model_id or "?"
    headers = _build_codex_headers(
        request,
        request_id=request_id,
        initiator=initiator,
        streaming=streaming,
    )
    if settings.log_bodies:
        logger.debug(
            "-> POST /codex/v1/responses model=%s body=%s",
            model,
            redact_bytes(body).decode("utf-8", "replace"),
        )
    upstream_url = f"{settings.api_base}/responses"
    if not streaming:
        response = await _send_non_streaming(
            client=client,
            url=upstream_url,
            body=body,
            headers=headers,
            request_id=request_id,
            route="Codex non-stream",
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "POST /codex/v1/responses model=%s status=%d duration_ms=%d request_id=%s stream=0",
            model,
            response.status_code,
            duration_ms,
            request_id,
        )
        return response
    return await _codex_stream_response(
        client=client,
        url=upstream_url,
        body=body,
        headers=headers,
        request=request,
        model=model,
        request_id=request_id,
        started=started,
    )


def _compact_input_with_prompt(input_value: object) -> list[dict[str, Any]]:
    if isinstance(input_value, list):
        input_items = [item for item in input_value if isinstance(item, dict)]
    elif isinstance(input_value, str):
        input_items = [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": input_value}],
        }]
    else:
        input_items = []
    input_items.append({
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": COMPACTION_PROMPT}],
    })
    return input_items


def _compact_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _compact_text_parts(parts: object, kind: str) -> bool:
    return isinstance(parts, list) and all(
        isinstance(part, dict) and part.get("type") == kind and isinstance(part.get("text"), str)
        for part in parts
    )


def _compact_usage_valid(usage: object) -> bool:
    # Responses usage is optional/nullable. Preserve partial provider accounting
    # objects, but validate every known count/detail that is actually supplied.
    if usage is None:
        return True
    if not isinstance(usage, dict):
        return False
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        if key in usage and not _compact_nonnegative_int(usage[key]):
            return False
    for key, counts in (
        ("input_tokens_details", ("cached_tokens", "cache_write_tokens")),
        ("output_tokens_details", ("reasoning_tokens",)),
    ):
        if key in usage:
            detail = usage[key]
            if not isinstance(detail, dict) or any(
                name in detail and not _compact_nonnegative_int(detail[name]) for name in counts
            ):
                return False
    return True


def _compact_annotation_valid(annotation: object) -> bool:
    if not isinstance(annotation, dict) or not isinstance(annotation.get("type"), str):
        return False
    fields = {
        "file_citation": (("file_id", "filename"), ("index",)),
        "file_path": (("file_id",), ("index",)),
        "url_citation": (("url", "title"), ("start_index", "end_index")),
        "container_file_citation": (("container_id", "file_id", "filename"), ("start_index", "end_index")),
    }.get(annotation["type"])
    if fields is None:
        return False
    strings, indexes = fields
    return all(isinstance(annotation.get(key), str) for key in strings) and all(
        _compact_nonnegative_int(annotation.get(key)) for key in indexes
    )


def _compact_logprob_valid(value: object, *, top_level: bool = True) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("token"), str):
        return False
    probability = value.get("logprob")
    if isinstance(probability, bool) or not (
        isinstance(probability, int) or isinstance(probability, float) and math.isfinite(probability)
    ):
        return False
    raw_bytes = value.get("bytes")
    if raw_bytes is not None and (
        not isinstance(raw_bytes, list)
        or any(not _compact_nonnegative_int(byte) or byte > 255 for byte in raw_bytes)
    ):
        return False
    if top_level:
        alternatives = value.get("top_logprobs", [])
        return isinstance(alternatives, list) and all(
            _compact_logprob_valid(item, top_level=False) for item in alternatives
        )
    return True


def _finite_json_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number.")
    return number


def _has_usable_compact_summary(result: object) -> bool:
    """Accept completed assistant text, not failed or unfinished tool output."""
    if (
        not isinstance(result, dict)
        or result.get("status") != "completed"
        or result.get("error") is not None
        or result.get("incomplete_details") is not None
        or not _compact_usage_valid(result.get("usage"))
    ):
        return False
    output = result.get("output")
    if not isinstance(output, list):
        return False
    has_summary = False
    for item in output:
        if not isinstance(item, dict):
            return False
        if "id" in item and not isinstance(item["id"], str):
            return False
        if item.get("type") == "reasoning":
            if (
                item.get("status") not in (None, "completed")
                or not _compact_text_parts(item.get("summary"), "summary_text")
                or ("content" in item and not _compact_text_parts(item["content"], "reasoning_text"))
                or (item.get("encrypted_content") is not None and not isinstance(item["encrypted_content"], str))
            ):
                return False
            continue
        if (
            item.get("type") != "message"
            or item.get("role") != "assistant"
            or item.get("status") != "completed"
            or item.get("phase") not in (None, "commentary", "final_answer")
        ):
            return False
        content = item.get("content")
        if not isinstance(content, list) or not content:
            return False
        for part in content:
            if (
                not isinstance(part, dict)
                or part.get("type") != "output_text"
                or not isinstance(part.get("text"), str)
            ):
                return False
            annotations = part.get("annotations", [])
            if not isinstance(annotations, list) or not all(_compact_annotation_valid(a) for a in annotations):
                return False
            logprobs = part.get("logprobs")
            if logprobs is not None and (
                not isinstance(logprobs, list) or not all(_compact_logprob_valid(value) for value in logprobs)
            ):
                return False
            has_summary = has_summary or bool(part["text"].strip())
    return has_summary


async def _synthetic_compact(
    *,
    request: Request,
    parsed: dict[str, Any],
    request_id: str,
) -> Response:
    settings = get_settings()
    client: httpx.AsyncClient = request.app.state.http_client
    model = parsed.get("model")
    if not isinstance(model, str) or not model:
        return openai_json_error("invalid_request_error", "Codex compact request requires a model.", status=400)

    payload: dict[str, Any] = {
        "model": model,
        "input": _compact_input_with_prompt(parsed.get("input")),
        "stream": False,
        "store": False,
    }
    for key in ("instructions", "reasoning", "text", "metadata", "client_metadata", "prompt_cache_key"):
        if key in parsed:
            payload[key] = parsed[key]
    body = _serialize_json_body(payload)
    headers = _build_codex_headers(request, request_id=request_id, initiator="agent", streaming=False)
    upstream = await _send_non_streaming(
        client=client,
        url=f"{settings.api_base}/responses",
        body=body,
        headers=headers,
        request_id=request_id,
        route="Codex synthetic compact",
    )
    if upstream.status_code >= 400:
        return upstream
    if not 200 <= upstream.status_code < 300:
        return openai_json_error("server_error", "Synthetic compact upstream returned a non-success status.", status=502)
    try:
        result = json.loads(
            upstream.body, parse_float=_finite_json_number, parse_constant=_finite_json_number,
        )
    except (ValueError, RecursionError):
        return openai_json_error("server_error", "Synthetic compact upstream returned non-JSON.", status=502)
    if not _has_usable_compact_summary(result):
        return openai_json_error(
            "server_error", "Synthetic compact upstream did not return a completed, usable summary.", status=502,
        )
    try:
        return JSONResponse({
            "id": f"resp_compact_{uuid.uuid4().hex[:24]}",
            "object": "response.compaction",
            "created_at": int(time.time()),
            "output": result["output"],
            "usage": result.get("usage", {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}),
        })
    except (TypeError, ValueError, RecursionError):
        return openai_json_error("server_error", "Synthetic compact upstream returned unserializable data.", status=502)


async def proxy_codex_responses_compact(request: Request) -> Response:
    settings = get_settings()
    client: httpx.AsyncClient = request.app.state.http_client
    request_id = str(uuid.uuid4())
    raw_body = await request.body()
    body, _model_id, _streaming, initiator = _parse_codex_request(raw_body, request_id=request_id)
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return openai_json_error("invalid_request_error", "Malformed JSON body.", status=400)
    if not isinstance(parsed, dict):
        return openai_json_error("invalid_request_error", "Request body must be a JSON object.", status=400)

    headers = _build_codex_headers(request, request_id=request_id, initiator=initiator, streaming=False)
    native = await _send_non_streaming(
        client=client,
        url=f"{settings.api_base}/responses/compact",
        body=body,
        headers=headers,
        request_id=request_id,
        route="Codex compact",
    )
    if native.status_code != 404:
        return native
    logger.debug("upstream /responses/compact returned 404; using synthetic compact request_id=%s", request_id)
    return await _synthetic_compact(request=request, parsed=parsed, request_id=request_id)


def _model_supports_responses(model: dict[str, Any]) -> bool:
    endpoints = model.get("supported_endpoints")
    return isinstance(endpoints, list) and "/responses" in endpoints


def _model_context_window(model: dict[str, Any]) -> int:
    capabilities = model.get("capabilities")
    limits = capabilities.get("limits") if isinstance(capabilities, dict) else None
    if isinstance(limits, dict):
        for key in ("max_context_window_tokens", "max_context_window", "context_window"):
            value = limits.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return 272_000


def _codex_reasoning_levels(model: dict[str, Any]) -> list[dict[str, str]]:
    capabilities = model.get("capabilities")
    supports = capabilities.get("supports") if isinstance(capabilities, dict) else None
    raw_efforts = supports.get("reasoning_effort") if isinstance(supports, dict) else None

    efforts: list[str] = []
    if isinstance(raw_efforts, list):
        seen: set[str] = set()
        for value in raw_efforts:
            if not isinstance(value, str):
                continue
            effort = value.strip().lower()
            if not effort or effort in seen:
                continue
            seen.add(effort)
            efforts.append(effort)
    if not efforts:
        efforts = list(_DEFAULT_CODEX_REASONING_EFFORTS)

    return [
        {
            "effort": effort,
            "description": _REASONING_EFFORT_DESCRIPTIONS.get(
                effort, f"{effort.title()} reasoning effort"
            ),
        }
        for effort in efforts
    ]


def _codex_input_modalities(model: dict[str, Any]) -> list[str]:
    capabilities = model.get("capabilities")
    supports = capabilities.get("supports") if isinstance(capabilities, dict) else None
    if isinstance(supports, dict) and supports.get("vision") is True:
        return ["text", "image"]
    return ["text"]


def _codex_model_info(model: dict[str, Any], model_id: str) -> dict[str, Any]:
    name = model.get("name") if isinstance(model.get("name"), str) else model_id
    context_window = _model_context_window(model)
    reasoning_levels = _codex_reasoning_levels(model)
    reasoning_efforts = {level["effort"] for level in reasoning_levels}
    default_reasoning_level = "medium" if "medium" in reasoning_efforts else reasoning_levels[0]["effort"]
    return {
        "slug": model_id,
        "display_name": name,
        "description": f"{name} via GitHub Copilot.",
        "default_reasoning_level": default_reasoning_level,
        "supported_reasoning_levels": reasoning_levels,
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 0,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "availability_nux": None,
        "upgrade": None,
        "base_instructions": (
            "You are Codex, a coding agent. Follow the user's instructions and use available tools safely."
        ),
        "model_messages": None,
        "supports_reasoning_summaries": False,
        "default_reasoning_summary": "none",
        "support_verbosity": True,
        "default_verbosity": "low",
        "apply_patch_tool_type": "freeform",
        "web_search_tool_type": "text",
        "truncation_policy": {"mode": "tokens", "limit": 10_000},
        "supports_parallel_tool_calls": True,
        "supports_image_detail_original": False,
        "context_window": context_window,
        "max_context_window": context_window,
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [],
        "input_modalities": _codex_input_modalities(model),
        "supports_search_tool": True,
    }


async def proxy_codex_models(request: Request) -> Response:
    settings = get_settings()
    client: httpx.AsyncClient = request.app.state.http_client
    request_id = str(uuid.uuid4())
    headers = _build_codex_headers(request, request_id=request_id, initiator="user", streaming=False)
    try:
        resp = await client.get(f"{settings.api_base}/models", headers=headers, timeout=30.0)
    except httpx.HTTPError as exc:
        return openai_json_error("server_error", f"Upstream /models error: {redact_text(str(exc))}")
    if resp.status_code != 200:
        return openai_json_error(
            _openai_kind_for_status(resp.status_code),
            f"Upstream /models {resp.status_code}: {redact_text(resp.text)[:300]}",
            status=resp.status_code,
        )
    try:
        payload = resp.json()
    except ValueError:
        return openai_json_error(
            "server_error",
            f"Upstream /models returned non-JSON body: {redact_text(resp.text)[:200]}",
        )
    raw = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return openai_json_error(
            "server_error", "Upstream /models returned an invalid model catalog; expected a data array.", status=502,
        )
    openai_items: list[dict[str, Any]] = []
    codex_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for model in raw:
        if not isinstance(model, dict) or not _model_supports_responses(model):
            continue
        mid = model.get("id")
        if not isinstance(mid, str) or not mid or mid in seen_ids:
            continue
        seen_ids.add(mid)
        openai_items.append({
            "id": mid,
            "object": "model",
            "created": 0,
            "owned_by": model.get("vendor") if isinstance(model.get("vendor"), str) else "github-copilot",
        })
        codex_items.append(_codex_model_info(model, mid))
    return JSONResponse({"object": "list", "data": openai_items, "models": codex_items})


async def codex_healthz(request: Request) -> Response:
    settings = get_settings()
    client: httpx.AsyncClient = request.app.state.http_client
    request_id = str(uuid.uuid4())
    upstream_ok = False
    upstream_status: int | None = None
    model_count = 0
    hint: str | None = None
    try:
        resp = await client.get(
            f"{settings.api_base}/models",
            headers=_build_codex_headers(request, request_id=request_id, initiator="user", streaming=False),
            timeout=10.0,
        )
        upstream_status = resp.status_code
        if resp.status_code == 200:
            try:
                payload = resp.json()
            except ValueError as exc:
                logger.warning("codex_healthz upstream 200 with non-JSON body: %s", exc)
            else:
                data = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(data, list):
                    raise ValueError("Upstream /models returned an invalid model catalog; expected a data array.")
                model_count = sum(1 for model in data if isinstance(model, dict) and _model_supports_responses(model))
                upstream_ok = True
        elif resp.status_code in (401, 403):
            hint = "token may be expired; re-run scripts/extract-token.ps1 then docker compose up -d --force-recreate proxy"
    except Exception as exc:
        logger.warning("codex_healthz upstream check failed: %s", redact_text(str(exc)))
    body: dict[str, Any] = {
        "ok": True,
        "upstream_ok": upstream_ok,
        "upstream_status": upstream_status,
        "response_models": model_count,
    }
    if hint:
        body["hint"] = hint
    return JSONResponse(body)
