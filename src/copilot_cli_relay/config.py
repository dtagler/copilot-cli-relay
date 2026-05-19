"""Runtime configuration parsed from environment variables."""
from __future__ import annotations

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
