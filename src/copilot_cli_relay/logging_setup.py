"""Structured logging helpers with secret/body redaction."""
from __future__ import annotations

import logging
import re

# GitHub OAuth/PAT tokens (gho_, ghu_, ghs_, ghr_, ghp_).
_TOKEN_RE = re.compile(r"\b(gh[ousrp]_)[A-Za-z0-9]{20,}")
# GitHub fine-grained personal access tokens.
_GITHUB_PAT_RE = re.compile(r"\b(github_pat_)[A-Za-z0-9_]{20,}")
# AWS secret access keys: 40 chars of [A-Za-z0-9/+=], typically appearing in a
# context like `aws_secret_access_key = …`. The standalone form is too generic
# to match safely; require a key-name prefix.
_AWS_SECRET_RE = re.compile(
    r"(?i)(aws[_-]?secret[_-]?access[_-]?key\s*[:=]\s*[\"']?)[A-Za-z0-9/+=]{40}"
)
# Slack tokens.
_SLACK_TOKEN_RE = re.compile(r"\b(xox[baprs]-)[A-Za-z0-9-]{10,}")
# Stripe live secret/restricted keys.
_STRIPE_KEY_RE = re.compile(r"\b((?:sk|rk)_live_)[A-Za-z0-9]{16,}")
# Google API keys.
_GOOGLE_API_KEY_RE = re.compile(r"\b(AIza)[A-Za-z0-9_\-]{35}")
# `Authorization: …` and `x-api-key: …` style header lines. Match everything
# after the colon up to a clear terminator (newline or JSON-value delimiters)
# so multi-token schemes like `Authorization: Bearer <token>` get fully redacted,
# not just the scheme word.
_HEADER_SECRET_RE = re.compile(
    r"(?i)((?:authorization|x-api-key|api-key|proxy-authorization)\s*:\s*)[^\r\n,;\"']+"
)
# Same auth-style headers but in dict-repr / JSON form, e.g. `{'Authorization': 'Basic ...'}`
# or `"x-api-key": "abc..."`. The plain header regex above can't match these because the
# value starts with a quote (a terminator). Captures the quoted value.
_HEADER_SECRET_QUOTED_RE = re.compile(
    r"(?i)([\"'](?:authorization|x-api-key|api-key|proxy-authorization)[\"']\s*[:=]\s*[\"'])[^\"'\r\n]+([\"'])"
)
# Bearer tokens that aren't on an Authorization-prefixed line (rare but possible
# inside JSON tool inputs, e.g. `"value": "Bearer abc..."`).
_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._\-+/=]{16,}")
# OpenAI / Anthropic-style sk- keys.
_SK_KEY_RE = re.compile(r"\b(sk-(?:proj-|ant-)?)[A-Za-z0-9_\-]{16,}")
# AWS access key ids.
_AWS_KEY_RE = re.compile(r"\b(AKIA)[0-9A-Z]{16}\b")
# JWTs / JWS — three base64url segments separated by dots.
_JWT_RE = re.compile(r"\b(eyJ[A-Za-z0-9_\-]{8,})\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")
# JSON-style key/value pairs whose key smells secret-y. Accepts both
# double-quoted (real JSON) and single-quoted (Python dict-repr leaking into
# logs via repr()/str() on a dict) forms; the same quote char is required on
# both ends of each delimited token.
_JSON_SECRET_RE = re.compile(
    r"(?i)(([\"'])(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|client[_-]?secret|refresh[_-]?token|private[_-]?key)\2\s*:\s*([\"']))[^\r\n]{6,}?(\3)"
)
_DATA_URI_RE = re.compile(rb"data:([^;]+);base64,([A-Za-z0-9+/=]{65,})")

MAX_BODY_BYTES = 16 * 1024


def configure_logging(level: str = "info") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def redact_text(text: str) -> str:
    text = _TOKEN_RE.sub(r"\1***REDACTED***", text)
    text = _GITHUB_PAT_RE.sub(r"\1***REDACTED***", text)
    text = _JWT_RE.sub(r"\1***REDACTED***", text)
    text = _SK_KEY_RE.sub(r"\1***REDACTED***", text)
    text = _AWS_KEY_RE.sub(r"\1***REDACTED***", text)
    text = _AWS_SECRET_RE.sub(r"\1***REDACTED***", text)
    text = _SLACK_TOKEN_RE.sub(r"\1***REDACTED***", text)
    text = _STRIPE_KEY_RE.sub(r"\1***REDACTED***", text)
    text = _GOOGLE_API_KEY_RE.sub(r"\1***REDACTED***", text)
    text = _HEADER_SECRET_RE.sub(r"\1***REDACTED***", text)
    text = _HEADER_SECRET_QUOTED_RE.sub(r"\1***REDACTED***\2", text)
    text = _BEARER_RE.sub(r"\1***REDACTED***", text)
    text = _JSON_SECRET_RE.sub(r"\1***REDACTED***\4", text)
    return text


def redact_bytes(body: bytes) -> bytes:
    body = _DATA_URI_RE.sub(
        lambda m: m.group(0)[:64] + b"...[truncated " + str(len(m.group(2))).encode() + b" b64 chars]",
        body,
    )
    if len(body) > MAX_BODY_BYTES:
        body = body[:MAX_BODY_BYTES] + b"...[truncated]"
    try:
        return redact_text(body.decode("utf-8", errors="replace")).encode("utf-8")
    except Exception:
        return body


logger = logging.getLogger("copilot_cli_relay")
