"""The drain rehearsal cannot pass from a completion sent before shutdown."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "shutdown_fixture", Path(__file__).resolve().parents[1] / "integration/stream_shutdown_fixture.py",
)
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


def test_drain_requires_observed_listener_shutdown():
    states = iter([True, True, False])
    fixture.wait_for_listener_shutdown(lambda: next(states), timeout=1, interval=0)


def test_drain_does_not_complete_just_because_time_passes():
    with pytest.raises(TimeoutError):
        fixture.wait_for_listener_shutdown(lambda: True, timeout=0, interval=0)


def test_observer_rejects_unsynchronized_completion():
    with pytest.raises(AssertionError, match="synchronized"):
        fixture.verify_completion(b'data: {"type":"response.completed"}\n\n')
    fixture.verify_completion(b'data: {"type":"response.completed","fixture_shutdown_observed":true}\n\n')
