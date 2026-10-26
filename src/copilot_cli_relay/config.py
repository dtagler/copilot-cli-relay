"""Runtime configuration parsed from environment variables."""
from __future__ import annotations

import json
import os
import secrets
import uuid
from dataclasses import dataclass, field

from . import __version__


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(name: str, default: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer between 1 and {maximum}.") from None
    if not 1 <= value <= maximum:
        raise ConfigError(f"{name} must be an integer between 1 and {maximum}.")
    return value


def _model_overrides() -> dict[str, dict[str, bool]]:
    raw = os.environ.get("COPILOT_CODEX_MODEL_OVERRIDES", "{}")
    message = "COPILOT_CODEX_MODEL_OVERRIDES must map model IDs to supported Boolean capability overrides."
    if len(raw) > 65536:
        raise ConfigError(message)
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise ConfigError(message) from None
    allowed = {"support_verbosity", "supports_parallel_tool_calls", "supports_search_tool"}
    if not isinstance(parsed, dict) or any(
        not isinstance(mid, str) or not mid or len(mid) > 128 or not mid.isprintable()
        or not isinstance(values, dict)
        or any(key not in allowed or type(value) is not bool for key, value in values.items())
        for mid, values in parsed.items()
    ):
        raise ConfigError(message)
    return parsed


@dataclass(frozen=True)
class Settings:
    proxy_port: int
    github_token: str
    api_base: str
    integration_id: str
    editor_version: str
    log_level: str
    log_bodies: bool
    codex_integration_id: str = "copilot-developer-cli"
    codex_editor_version: str = "vscode/1.99.0"
    codex_plugin_version: str = "copilot-chat/0.43.2026033101"
    codex_user_agent: str = "GitHubCopilotChat/0.43.2026033101"
    codex_github_api_version: str = "2026-01-09"
    codex_session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    codex_machine_id: str = field(default_factory=lambda: secrets.token_hex(32))
    max_request_bytes: int = 16 * 1024 * 1024
    max_response_bytes: int = 32 * 1024 * 1024
    max_catalog_bytes: int = 2 * 1024 * 1024
    max_inflight: int = 8
    request_body_timeout: int = 30
    request_timeout: int = 900
    max_catalog_models: int = 512
    enable_synthetic_compaction: bool = False
    codex_model_overrides: dict[str, dict[str, bool]] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Settings:
        token = os.environ.get("COPILOT_GITHUB_TOKEN", "").strip()
        if not token:
            raise ConfigError(
                "COPILOT_GITHUB_TOKEN is required. On the host run "
                "`pwsh scripts/extract-token.ps1` to populate .env from the "
                "Copilot CLI credential, then `docker compose up`."
            )
        if not (token.startswith("gho_") or token.startswith("ghu_")):
            raise ConfigError(
                "COPILOT_GITHUB_TOKEN does not look like a GitHub OAuth token "
                "(expected gho_… or ghu_… prefix)."
            )
        port_raw = os.environ.get("COPILOT_PROXY_PORT", "4141")
        try:
            port = int(port_raw)
        except ValueError as e:
            raise ConfigError(
                f"COPILOT_PROXY_PORT must be an integer 1..65535, got {port_raw!r}."
            ) from e
        if not 1 <= port <= 65535:
            raise ConfigError(
                f"COPILOT_PROXY_PORT must be in 1..65535, got {port}."
            )
        api_base = os.environ.get("COPILOT_API_BASE", "https://api.githubcopilot.com").rstrip("/")
        # Refuse non-https api_base unless explicit opt-in. The proxy injects
        # the Copilot OAuth bearer into every outbound request — sending it
        # over plaintext (or to an unintended host) would leak the credential.
        # Tests/mocks set COPILOT_ALLOW_INSECURE_API_BASE=1 to permit http://.
        if not api_base.lower().startswith("https://") and not _bool(
            os.environ.get("COPILOT_ALLOW_INSECURE_API_BASE")
        ):
            raise ConfigError(
                f"COPILOT_API_BASE must use https:// (got {api_base!r}). "
                "Set COPILOT_ALLOW_INSECURE_API_BASE=1 to override (mocks/tests only)."
            )
        return cls(
            proxy_port=port,
            github_token=token,
            api_base=api_base,
            integration_id=os.environ.get("COPILOT_INTEGRATION_ID", "copilot-developer-cli"),
            editor_version=os.environ.get("COPILOT_EDITOR_VERSION", f"copilot-cli-relay/{__version__}"),
            log_level=os.environ.get("LOG_LEVEL", "info").lower(),
            log_bodies=_bool(os.environ.get("LOG_BODIES")),
            codex_integration_id=os.environ.get(
                "COPILOT_CODEX_INTEGRATION_ID",
                os.environ.get("COPILOT_INTEGRATION_ID", "copilot-developer-cli"),
            ),
            codex_editor_version=os.environ.get("COPILOT_CODEX_EDITOR_VERSION", "vscode/1.99.0"),
            codex_plugin_version=os.environ.get(
                "COPILOT_CODEX_PLUGIN_VERSION",
                "copilot-chat/0.43.2026033101",
            ),
            codex_user_agent=os.environ.get(
                "COPILOT_CODEX_USER_AGENT",
                "GitHubCopilotChat/0.43.2026033101",
            ),
            codex_github_api_version=os.environ.get("COPILOT_CODEX_GITHUB_API_VERSION", "2026-01-09"),
            codex_session_id=os.environ.get("COPILOT_CODEX_SESSION_ID", str(uuid.uuid4())),
            codex_machine_id=os.environ.get("COPILOT_CODEX_MACHINE_ID", secrets.token_hex(32)),
            max_request_bytes=_positive_int("COPILOT_MAX_REQUEST_BYTES", 16 * 1024 * 1024, 128 * 1024 * 1024),
            max_response_bytes=_positive_int("COPILOT_MAX_RESPONSE_BYTES", 32 * 1024 * 1024, 128 * 1024 * 1024),
            max_catalog_bytes=_positive_int("COPILOT_MAX_CATALOG_BYTES", 2 * 1024 * 1024, 16 * 1024 * 1024),
            max_inflight=_positive_int("COPILOT_MAX_INFLIGHT", 8, 128),
            request_body_timeout=_positive_int("COPILOT_REQUEST_BODY_TIMEOUT", 30, 600),
            request_timeout=_positive_int("COPILOT_REQUEST_TIMEOUT", 900, 7200),
            max_catalog_models=_positive_int("COPILOT_MAX_CATALOG_MODELS", 512, 4096),
            enable_synthetic_compaction=_bool(os.environ.get("COPILOT_ENABLE_SYNTHETIC_COMPACTION")),
            codex_model_overrides=_model_overrides(),
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reset_settings_for_tests(s: Settings | None = None) -> None:
    """Tests inject a Settings object without going through env vars."""
    global _settings
    _settings = s
