"""
tests/unit/scripts/test_scenarios.py
──────────────────────────────────────
Unit tests for the DB-/stack-free logic of the demo scenarios under
scripts/scenarios/. These prove the shared plumbing in _common.py works and
that every scenario module imports cleanly and exposes a `main`. They do NOT
touch Redis, HTTP, or the docker-compose stack — that's what running the
scripts against the live stack is for.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCENARIOS = Path(__file__).resolve().parents[2].parent / "scripts" / "scenarios"
if str(_SCENARIOS) not in sys.path:
    sys.path.insert(0, str(_SCENARIOS))

import _common as C  # noqa: E402


# ── connection defaults match the integration conftest + SDK ──────────────────

def test_default_connection_urls(monkeypatch):
    for var in (
        "REDIS_URL", "POLICY_URL", "SMARTLOAD_AUTOSCALER_URL",
        "SMARTLOAD_ANOMALY_DETECTOR_URL", "SMARTLOAD_FORECASTING_URL",
        "SMARTLOAD_OPERATOR_UI_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    assert C.redis_url() == "redis://localhost:6379"
    assert C.policy_url() == "http://localhost:8086"
    assert C.autoscaler_url() == "http://localhost:8085"
    assert C.anomaly_detector_url() == "http://localhost:8082"
    assert C.forecasting_url() == "http://localhost:8083"
    assert C.operator_ui_url() == "http://localhost:8090"


def test_connection_urls_honor_env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379")
    monkeypatch.setenv("POLICY_URL", "http://policy-manager:8086")
    assert C.redis_url() == "redis://redis:6379"
    assert C.policy_url() == "http://policy-manager:8086"


# ── narration helpers return the documented exit codes ────────────────────────

def test_fail_returns_one_and_writes_stderr(capsys):
    rc = C.fail("boom")
    captured = capsys.readouterr()
    assert rc == 1
    assert "FAIL: boom" in captured.err


def test_done_returns_zero(capsys):
    rc = C.done("all good")
    captured = capsys.readouterr()
    assert rc == 0
    assert "PASS" in captured.out
    assert "all good" in captured.out


# ── wait_for_envelope: a fake pubsub drives the canonical decode path ─────────

class _FakePubSub:
    """Minimal pubsub stand-in: yields queued raw messages then None (timeout)."""

    def __init__(self, messages):
        self._messages = list(messages)

    def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        if self._messages:
            return self._messages.pop(0)
        return None


def _envelope_msg(channel, payload):
    """Build a Redis-style message dict carrying a canonical envelope."""
    from services.shared.contracts import make_envelope
    from dataclasses import asdict

    env = make_envelope(source="unit-test", payload=payload)
    return {"type": "message", "data": json.dumps(asdict(env)).encode()}


def test_wait_for_envelope_matches_predicate():
    msgs = [
        _envelope_msg("smartload.scale", {"action": "scale_in", "instance_count": 2}),
        _envelope_msg("smartload.scale", {"action": "scale_out", "instance_count": 4}),
    ]
    result = C.wait_for_envelope(
        _FakePubSub(msgs),
        "smartload.scale",
        lambda p, _m: p.get("action") == "scale_out",
        timeout=2.0,
    )
    assert result is not None
    payload, meta = result
    assert payload["action"] == "scale_out"
    assert payload["instance_count"] == 4
    assert meta["source"] == "unit-test"


def test_wait_for_envelope_times_out_when_no_match():
    msgs = [_envelope_msg("smartload.scale", {"action": "scale_in", "instance_count": 1})]
    result = C.wait_for_envelope(
        _FakePubSub(msgs),
        "smartload.scale",
        lambda p, _m: p.get("action") == "scale_out",
        timeout=0.2,
    )
    assert result is None


def test_wait_for_envelope_ignores_non_message_frames():
    msgs = [
        {"type": "subscribe", "data": 1},
        _envelope_msg("smartload.policy", {"safe_mode": True}),
    ]
    result = C.wait_for_envelope(
        _FakePubSub(msgs),
        "smartload.policy",
        lambda p, _m: p.get("safe_mode") is True,
        timeout=2.0,
    )
    assert result is not None
    assert result[0]["safe_mode"] is True


def test_wait_for_envelope_survives_a_bad_predicate():
    """A predicate that raises must not crash the poll; the message is skipped."""
    msgs = [_envelope_msg("smartload.scale", {"action": "scale_out"})]

    def _boom(_p, _m):
        raise RuntimeError("predicate blew up")

    result = C.wait_for_envelope(
        _FakePubSub(msgs), "smartload.scale", _boom, timeout=0.2,
    )
    assert result is None


# ── every scenario module imports cleanly and exposes main() ──────────────────

@pytest.mark.parametrize(
    "module_name",
    [
        "forecast_burst",
        "anomaly_inject",
        "safe_mode_toggle",
        "policy_walk",
        "scale_to_n",
        "consolidated_status",
    ],
)
def test_scenario_module_is_importable_with_main(module_name):
    import importlib

    mod = importlib.import_module(module_name)
    assert hasattr(mod, "main"), f"{module_name} is missing main()"
    assert callable(mod.main)


def test_consolidated_status_local_rollup():
    """The status demo reimplements the overall pill rollup; check the logic."""
    import importlib

    cs = importlib.import_module("consolidated_status")
    from smartload_client import ServiceStatus

    def svc(status):
        return ServiceStatus(name="x", status=status)

    assert cs._expected_overall({"a": svc("ok"), "b": svc("ok")}) == "ok"
    assert cs._expected_overall({"a": svc("ok"), "b": svc("degraded")}) == "degraded"
    assert cs._expected_overall({"a": svc("down"), "b": svc("degraded")}) == "down"
