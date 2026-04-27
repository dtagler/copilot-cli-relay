"""Runtime configuration parsed from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass

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
            editor_version=os.environ.get("COPILOT_EDITOR_VERSION", f"claude-copilot-cli-relay/{__version__}"),
            log_level=os.environ.get("LOG_LEVEL", "info").lower(),
            log_bodies=_bool(os.environ.get("LOG_BODIES")),
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
