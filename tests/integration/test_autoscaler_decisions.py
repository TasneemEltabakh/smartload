"""
tests/integration/test_autoscaler_decisions.py
───────────────────────────────────────────────
Pure-Python tests for services/autoscaler/decisions.py. No docker stack
required — runs in the unit-tests CI job alongside test_telemetry_parser.py
and test_s2_baseline.py.

Exercises the decision matrix from SOT §8.8 Logic:
  - scale_out when predicted_rps > current_count × per_instance_capacity_rps
  - scale_in  when predicted_rps < (current_count − 1) × capacity
  - noop      within the band, at bounds, or during cooldown
  - reason strings tagged so audit log distinguishes forecast vs reactive
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

# Load services/autoscaler/decisions.py without making it a package, same
# convention as test_telemetry_parser.py.
_REPO = pathlib.Path(__file__).resolve().parents[2]
_MOD  = _REPO / "services" / "autoscaler" / "decisions.py"

_spec = importlib.util.spec_from_file_location("autoscaler_decisions", _MOD)
decisions = importlib.util.module_from_spec(_spec)
sys.modules["autoscaler_decisions"] = decisions
_spec.loader.exec_module(decisions)


def _policy(min_b=1, max_b=5, cap=100.0, cooldown=60.0):
    return decisions.Policy(
        min_backends=min_b,
        max_backends=max_b,
        per_instance_capacity_rps=cap,
        cooldown_seconds=cooldown,
    )


# ── scale_out path ────────────────────────────────────────────────────────────

class TestScaleOut:

    def test_predicted_exceeds_capacity_scales_out(self):
        d = decisions.decide(
            predicted_rps=250.0,
            current_count=2,
            policy=_policy(),
            seconds_since_last_action=None,
        )
        assert d.action == decisions.ACTION_SCALE_OUT
        assert d.target_count == 3
        assert "forecast" in d.reason

    def test_at_max_backends_returns_noop_even_when_predicted_high(self):
        d = decisions.decide(
            predicted_rps=999.0,
            current_count=5,
            policy=_policy(),
            seconds_since_last_action=None,
        )
        assert d.action == decisions.ACTION_NOOP
        assert d.target_count == 5
        assert "max_backends" in d.reason

    def test_cooldown_active_suppresses_scale_out(self):
        d = decisions.decide(
            predicted_rps=300.0,
            current_count=2,
            policy=_policy(cooldown=60.0),
            seconds_since_last_action=30.0,
        )
        assert d.action == decisions.ACTION_NOOP
        assert "cooldown" in d.reason

    def test_cooldown_just_elapsed_allows_scale_out(self):
        d = decisions.decide(
            predicted_rps=300.0,
            current_count=2,
            policy=_policy(cooldown=60.0),
            seconds_since_last_action=61.0,
        )
        assert d.action == decisions.ACTION_SCALE_OUT
        assert d.target_count == 3


# ── scale_in path ─────────────────────────────────────────────────────────────

class TestScaleIn:

    def test_predicted_under_shed_capacity_scales_in(self):
        # 4 backends × 100 rps = 400 capacity; shedding one leaves 300.
        # 250 < 300 → safe to shed one.
        d = decisions.decide(
            predicted_rps=250.0,
            current_count=4,
            policy=_policy(),
            seconds_since_last_action=None,
        )
        assert d.action == decisions.ACTION_SCALE_IN
        assert d.target_count == 3

    def test_at_min_backends_returns_noop_even_when_predicted_low(self):
        # With min=2, current=2, shed-capacity = (2-1)×100 = 100. predicted=50
        # is below that, so the scale_in branch is entered — and then bounded
        # back to noop by the min_backends guard.
        d = decisions.decide(
            predicted_rps=50.0,
            current_count=2,
            policy=_policy(min_b=2),
            seconds_since_last_action=None,
        )
        assert d.action == decisions.ACTION_NOOP
        assert "min_backends" in d.reason

    def test_cooldown_active_suppresses_scale_in(self):
        d = decisions.decide(
            predicted_rps=50.0,
            current_count=4,
            policy=_policy(cooldown=60.0),
            seconds_since_last_action=20.0,
        )
        assert d.action == decisions.ACTION_NOOP
        assert "cooldown" in d.reason


# ── no-op band ────────────────────────────────────────────────────────────────

class TestNoopBand:

    @pytest.mark.parametrize("predicted_rps", [200.0, 250.0, 299.0])
    def test_within_band_returns_noop(self, predicted_rps):
        # 3 backends × 100 = 300 capacity ceiling.
        # 2 backends × 100 = 200 shed-capacity floor.
        # Anything in [200, 300] is "current capacity is right".
        d = decisions.decide(
            predicted_rps=predicted_rps,
            current_count=3,
            policy=_policy(),
            seconds_since_last_action=None,
        )
        assert d.action == decisions.ACTION_NOOP
        assert "within band" in d.reason


# ── reactive-fallback tagging ────────────────────────────────────────────────

class TestReasonTagging:

    def test_reactive_label_propagates_to_reason(self):
        d = decisions.decide(
            predicted_rps=300.0,
            current_count=2,
            policy=_policy(),
            seconds_since_last_action=None,
            now_text="reactive",
        )
        assert d.action == decisions.ACTION_SCALE_OUT
        assert d.reason.startswith("reactive ")
