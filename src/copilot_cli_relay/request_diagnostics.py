"""Content-free request size diagnostics; never mutate or truncate history."""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass

from starlette.requests import Request

from .logging_setup import logger

MAX_DIAGNOSTIC_JSON_CHUNKS = 8192


@dataclass
class _EstimateBudget:
    remaining: int


def relay_request_id(request: Request) -> str:
    # Middleware owns this state, not an incoming header.
    value = getattr(request.state, "relay_request_id", None)
    return value if isinstance(value, str) else str(uuid.uuid4())


def model_log_label(model: str | None) -> str:
    # Do not log arbitrary model identifiers (they can contain user input).
    if model is not None:
        for prefix in ("gpt-", "claude-"):
            if model.startswith(prefix):
                return prefix[:-1]
    return "other"


def _json_size(value: object, *, budget: _EstimateBudget | None = None) -> int:
    # UTF-8 compact JSON estimates, not an accounting of original whitespace.
    # Surrogate escapes keep malformed Unicode from creating a new failure path.
    if budget is None:
        budget = _EstimateBudget(MAX_DIAGNOSTIC_JSON_CHUNKS)
    encoder = json.JSONEncoder(separators=(",", ":"), ensure_ascii=False)
    size = 0
    for chunk in encoder.iterencode(value):
        budget.remaining -= 1
        if budget.remaining < 0:
            raise ValueError("Diagnostic work budget exhausted.")
        size += len(chunk.encode("utf-8", "backslashreplace"))
    return size


def log_codex_request_size(
    parsed: object, *, incoming_bytes: int, outgoing_bytes: int, limit_bytes: int, request_id: str,
) -> None:
    if not logger.isEnabledFor(logging.INFO):
        return
    sizes = dict.fromkeys(("messages", "reasoning", "tool_calls", "tool_results", "other"), 0)
    item_count = 0
    tools_bytes = 0
    instructions_bytes = 0
    estimates_available = False
    model_family = "other"
    effort = "unknown"
    # One shared budget includes nested item fields, tools and instructions.
    # A large array of tiny items must not stall the single server event loop.
    budget = _EstimateBudget(MAX_DIAGNOSTIC_JSON_CHUNKS)
    try:
        if isinstance(parsed, dict):
            model = parsed.get("model")
            model_family = model_log_label(model if isinstance(model, str) else None)
            reasoning = parsed.get("reasoning")
            raw_effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
            if isinstance(raw_effort, str) and raw_effort in {
                "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
            }:
                effort = raw_effort
            items = parsed.get("input")
            if isinstance(items, str):
                items = [items]
            if isinstance(items, list):
                item_count = len(items)
                for item in items:
                    kind = item.get("type") if isinstance(item, dict) else None
                    if kind == "reasoning":
                        category = "reasoning"
                    elif kind in ("function_call", "custom_tool_call"):
                        category = "tool_calls"
                    elif kind in ("function_call_output", "custom_tool_call_output"):
                        category = "tool_results"
                    elif isinstance(item, str) or kind == "message" or (
                        isinstance(item, dict) and item.get("role") in ("user", "assistant", "system", "developer")
                    ):
                        category = "messages"
                    else:
                        category = "other"
                    sizes[category] += _json_size(item, budget=budget)
            tools_bytes = _json_size(parsed["tools"], budget=budget) if "tools" in parsed else 0
            instructions_bytes = _json_size(parsed["instructions"], budget=budget) if "instructions" in parsed else 0
            estimates_available = True
    except (ValueError, TypeError, RecursionError, OverflowError):
        # Diagnostics must not reject an otherwise forwardable body.
        sizes = dict.fromkeys(sizes, 0)
        tools_bytes = instructions_bytes = 0
    logger.info(
        "Codex request_size request_id=%s incoming_bytes=%d outgoing_bytes=%d limit_bytes=%d "
        "near_limit=%d model_family=%s reasoning_effort=%s estimates_available=%d input_items=%d "
        "messages_json_bytes=%d reasoning_json_bytes=%d tool_calls_json_bytes=%d "
        "tool_results_json_bytes=%d other_json_bytes=%d tools_json_bytes=%d instructions_json_bytes=%d",
        request_id, incoming_bytes, outgoing_bytes, limit_bytes,
        max(incoming_bytes, outgoing_bytes) >= limit_bytes * 4 // 5,
        model_family, effort, estimates_available, item_count,
        sizes["messages"], sizes["reasoning"], sizes["tool_calls"], sizes["tool_results"], sizes["other"],
        tools_bytes, instructions_bytes,
    )
