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


# ── policy_from_payload (T1.4 live-reload helper) ─────────────────────────────
#
# Tests for decisions.policy_from_payload — the pure helper the autoscaler
# uses to translate a PolicyUpdate envelope payload into a Policy dataclass.
# Lives here (not test_policy_validation) because it owns the autoscaler-side
# semantics: which fields are read, how missing/garbled ones fall back, and
# the type coercion rules. Validation belongs upstream in policy-manager.

class TestPolicyFromPayload:

    def _fallback(self):
        return _policy(min_b=1, max_b=5, cap=100.0, cooldown=60.0)

    def test_full_payload_yields_exact_policy(self):
        payload = {
            "min_backends": 2,
            "max_backends": 10,
            "per_instance_capacity_rps": 150,
            "autoscaler_cooldown_seconds": 30,
            "operating_mode": "hybrid",
            "safe_mode": False,
        }
        new = decisions.policy_from_payload(payload, fallback=self._fallback())
        assert new.min_backends == 2
        assert new.max_backends == 10
        assert new.per_instance_capacity_rps == 150.0
        assert new.cooldown_seconds == 30.0

    def test_missing_fields_fall_back(self):
        # Partial publish — autoscaler must keep current bounds for any field
        # the publisher omitted, not zero-out the scaling guards.
        fallback = self._fallback()
        new = decisions.policy_from_payload({"max_backends": 7}, fallback=fallback)
        assert new.max_backends == 7
        assert new.min_backends == fallback.min_backends
        assert new.per_instance_capacity_rps == fallback.per_instance_capacity_rps
        assert new.cooldown_seconds == fallback.cooldown_seconds

    def test_garbled_value_falls_back(self):
        # A misbehaving publisher could ship a string where we expect int.
        # policy_from_payload coerces what it can and falls back on the rest,
        # so the autoscaler keeps working with valid bounds.
        fallback = self._fallback()
        new = decisions.policy_from_payload(
            {"min_backends": "not-a-number", "max_backends": "8"},
            fallback=fallback,
        )
        assert new.min_backends == fallback.min_backends   # coercion failed → fallback
        assert new.max_backends == 8                       # string "8" coerces fine

    def test_unknown_fields_ignored(self):
        # Forward-compat: a new field appears in PolicyUpdate before the
        # autoscaler knows about it. Must not crash.
        new = decisions.policy_from_payload(
            {"experimental_flag": "on"}, fallback=self._fallback(),
        )
        # Result identical to the fallback (no scaling fields in payload).
        assert new == self._fallback()
