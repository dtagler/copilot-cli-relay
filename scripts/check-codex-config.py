"""Parse actual helper output supplied on stdin; never read user configuration."""
from __future__ import annotations

import json
import sys
import tomllib


def main() -> None:
    fixtures = json.load(sys.stdin)
    assert len(fixtures) == 4, "Expected CLI plus three desktop provider fixtures"
    for fixture in fixtures:
        config = tomllib.loads(fixture["config"])
        provider_id = fixture["provider"]
        assert set(config["model_providers"]) == {provider_id}
        provider = config["model_providers"][provider_id]
        assert provider["capabilities"]["remote_compaction"] == "unsupported"
        assert provider["wire_api"] == "responses"
        assert provider["base_url"] == "http://127.0.0.1:4141/codex/v1"
        assert "capabilities" not in provider["auth"]
        assert provider["auth"]["timeout_ms"] == 5000
        assert provider["auth"]["refresh_interval_ms"] == 0
        assert provider["auth"]["args"][-1] == "[Console]::Out.Write('dummy')"
        if provider_id == "copilot":
            assert provider["requires_openai_auth"] is False
            assert provider["stream_idle_timeout_ms"] == 300000
        else:
            assert config["model_provider"] == provider_id
            assert config["model"] == 'fixture-"quoted"\\variant'
            assert provider["supports_websockets"] is False
            assert provider["model_catalog_url"].endswith("/codex/v1/models")
    print("Four generated Codex provider configurations passed semantic TOML checks.")


if __name__ == "__main__":
    main()
