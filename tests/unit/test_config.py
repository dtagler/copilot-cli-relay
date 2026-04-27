import pytest

from claude_copilot_cli_relay.config import (
    ConfigError,
    Settings,
    _bool,
    get_settings,
    reset_settings_for_tests,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_settings_for_tests(None)
    yield
    reset_settings_for_tests(None)


def _set_env(monkeypatch, **kw):
    for k in (
        "COPILOT_GITHUB_TOKEN",
        "COPILOT_PROXY_PORT",
        "COPILOT_API_BASE",
        "COPILOT_ALLOW_INSECURE_API_BASE",
        "COPILOT_INTEGRATION_ID",
        "COPILOT_EDITOR_VERSION",
        "LOG_LEVEL",
        "LOG_BODIES",
    ):
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_missing_token_raises(monkeypatch):
    _set_env(monkeypatch)
    with pytest.raises(ConfigError, match="COPILOT_GITHUB_TOKEN is required"):
        Settings.from_env()


def test_blank_token_raises(monkeypatch):
    _set_env(monkeypatch, COPILOT_GITHUB_TOKEN="   ")
    with pytest.raises(ConfigError, match="COPILOT_GITHUB_TOKEN is required"):
        Settings.from_env()


def test_bad_token_prefix_raises(monkeypatch):
    _set_env(monkeypatch, COPILOT_GITHUB_TOKEN="ghp_personalAccessToken")
    with pytest.raises(ConfigError, match="does not look like a GitHub OAuth token"):
        Settings.from_env()


def test_invalid_port_raises_config_error(monkeypatch):
    _set_env(monkeypatch, COPILOT_GITHUB_TOKEN="gho_x", COPILOT_PROXY_PORT="not-a-number")
    with pytest.raises(ConfigError, match="must be an integer"):
        Settings.from_env()


def test_out_of_range_port_raises_config_error(monkeypatch):
    _set_env(monkeypatch, COPILOT_GITHUB_TOKEN="gho_x", COPILOT_PROXY_PORT="0")
    with pytest.raises(ConfigError, match="must be in 1..65535"):
        Settings.from_env()
    _set_env(monkeypatch, COPILOT_GITHUB_TOKEN="gho_x", COPILOT_PROXY_PORT="70000")
    with pytest.raises(ConfigError, match="must be in 1..65535"):
        Settings.from_env()


def test_non_https_api_base_rejected(monkeypatch):
    """The proxy injects the OAuth bearer into every outbound request — sending
    it over plaintext or to an unintended host would leak the credential."""
    _set_env(monkeypatch, COPILOT_GITHUB_TOKEN="gho_x", COPILOT_API_BASE="http://api.githubcopilot.com")
    with pytest.raises(ConfigError, match="must use https"):
        Settings.from_env()


def test_non_https_api_base_allowed_with_explicit_optin(monkeypatch):
    """Mocks/tests can override via COPILOT_ALLOW_INSECURE_API_BASE=1."""
    _set_env(
        monkeypatch,
        COPILOT_GITHUB_TOKEN="gho_x",
        COPILOT_API_BASE="http://localhost:8080",
        COPILOT_ALLOW_INSECURE_API_BASE="1",
    )
    s = Settings.from_env()
    assert s.api_base == "http://localhost:8080"


def test_defaults(monkeypatch):
    _set_env(monkeypatch, COPILOT_GITHUB_TOKEN="gho_abc")
    s = Settings.from_env()
    assert s.proxy_port == 4141
    assert s.github_token == "gho_abc"
    assert s.api_base == "https://api.githubcopilot.com"
    assert s.integration_id == "copilot-developer-cli"
    assert s.editor_version == "claude-copilot-cli-relay/0.1.0"
    assert s.log_level == "info"
    assert s.log_bodies is False


def test_overrides(monkeypatch):
    _set_env(
        monkeypatch,
        COPILOT_GITHUB_TOKEN="ghu_abc",
        COPILOT_PROXY_PORT="9999",
        COPILOT_API_BASE="https://example.test/",
        COPILOT_INTEGRATION_ID="vscode-chat",
        COPILOT_EDITOR_VERSION="my-editor/2",
        LOG_LEVEL="DEBUG",
        LOG_BODIES="yes",
    )
    s = Settings.from_env()
    assert s.proxy_port == 9999
    assert s.github_token == "ghu_abc"
    assert s.api_base == "https://example.test"  # trailing slash stripped
    assert s.integration_id == "vscode-chat"
    assert s.editor_version == "my-editor/2"
    assert s.log_level == "debug"
    assert s.log_bodies is True


def test_get_settings_caches(monkeypatch):
    _set_env(monkeypatch, COPILOT_GITHUB_TOKEN="gho_abc")
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2


def test_reset_settings_for_tests_injects_object():
    s = Settings(
        proxy_port=1, github_token="gho_x", api_base="x", integration_id="x",
        editor_version="x", log_level="info", log_bodies=False,
    )
    reset_settings_for_tests(s)
    assert get_settings() is s


@pytest.mark.parametrize(
    "raw,default,expected",
    [
        (None, False, False),
        (None, True, True),
        ("1", False, True),
        ("true", False, True),
        ("YES", False, True),
        ("On", False, True),
        ("0", True, False),
        ("false", True, False),
        ("  ", True, False),
    ],
)
def test_bool_parser(raw, default, expected):
    assert _bool(raw, default) is expected
