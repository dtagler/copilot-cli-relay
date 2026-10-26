"""Offline lock/environment checks fail closed without installing dependencies."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("drift", [False, True])
def test_lock_gate_detects_manifest_drift_without_mutation(tmp_path, drift):
    manifest = (ROOT / "pyproject.toml").read_text()
    if drift:
        manifest = manifest.replace("starlette>=1.0", "starlette>=2.0")
    (tmp_path / "pyproject.toml").write_text(manifest)
    lock = (ROOT / "uv.lock").read_bytes()
    (tmp_path / "uv.lock").write_bytes(lock)
    result = subprocess.run(
        ["uv", "lock", "--check", "--offline", "--project", str(tmp_path)],
        capture_output=True, timeout=15, env={**os.environ, "UV_CACHE_DIR": str(tmp_path / "cache")},
    )
    assert (result.returncode == 0) is not drift
    assert (tmp_path / "uv.lock").read_bytes() == lock
    assert not (tmp_path / ".venv").exists()


def test_environment_gate_fails_without_installing_a_missing_environment(tmp_path):
    for name in ("pyproject.toml", "uv.lock"):
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())
    result = subprocess.run(
        ["uv", "sync", "--check", "--frozen", "--offline", "--no-install-project", "--inexact",
         "--project", str(tmp_path)],
        capture_output=True, timeout=15, env={**os.environ, "UV_CACHE_DIR": str(tmp_path / "cache")},
    )
    assert result.returncode != 0
    assert not (tmp_path / ".venv").exists()
