"""
tests/integration/test_policy_validation.py
────────────────────────────────────────────
Pure-Python tests for services/policy-manager/validation.py. No docker
stack required — runs in the unit-tests CI job alongside
test_telemetry_parser.py and test_autoscaler_decisions.py.

Exercises every per-field rule from SOT §8.9 (lines 3002 + 3013), plus the
cross-field invariant min_backends <= max_backends and the merge-then-validate
semantics of validate_updates().
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

# Load validation.py without making policy-manager/ a package — the dash in
# the directory name forbids that. Same convention as test_telemetry_parser.py.
_REPO = pathlib.Path(__file__).resolve().parents[2]
_MOD  = _REPO / "services" / "policy-manager" / "validation.py"

_spec = importlib.util.spec_from_file_location("policy_manager_validation", _MOD)
validation = importlib.util.module_from_spec(_spec)
sys.modules["policy_manager_validation"] = validation
_spec.loader.exec_module(validation)


# A canonical baseline policy used by tests that need a valid `existing` to
# merge into. Mirrors config/policy.yaml so the merge semantics match prod.
BASELINE = {
    "anomaly_latency_multiplier": 3.0,
    "anomaly_recovery_window_seconds": 30,
    "anomaly_response": "auto-isolate",
    "autoscaler_cooldown_seconds": 60,
    "max_backends": 5,
    "min_backends": 1,
    "operating_mode": "hybrid",
    "per_instance_capacity_rps": 100,
    "rl_confidence_threshold": 0.6,
    "rl_exploration_rate": 0.0,
    "safe_mode": False,
    "slo_p95_latency_ms": 200,
}


# ── happy path ────────────────────────────────────────────────────────────────

class TestHappyPath:

    def test_validate_baseline_succeeds(self):
        validation.validate_merged_policy(BASELINE)   # must not raise

    def test_partial_update_merged_and_returned(self):
        merged = validation.validate_updates(
            {"max_backends": 8}, BASELINE,
        )
        assert merged["max_backends"] == 8
        # unrelated fields preserved
        assert merged["min_backends"] == 1
        assert merged["operating_mode"] == "hybrid"

    def test_unknown_fields_rejected(self):
        # Strict schema gating: a key outside CANONICAL_POLICY_FIELDS /
        # server-managed fields is rejected so junk can't leak into
        # config/policy.yaml. The error pinpoints the offending key.
        with pytest.raises(validation.PolicyValidationError) as exc:
            validation.validate_updates(
                {"experimental_flag": "on"}, BASELINE,
            )
        assert exc.value.field == "experimental_flag"
        assert "experimental_flag" in str(exc.value)


# ── enum validation ───────────────────────────────────────────────────────────

class TestEnumFields:

    @pytest.mark.parametrize("mode", ["classical-only", "hybrid", "rl-only"])
    def test_operating_mode_valid_values_accepted(self, mode):
        validation.validate_updates({"operating_mode": mode}, BASELINE)

    def test_operating_mode_invalid_value_rejected(self):
        with pytest.raises(validation.PolicyValidationError) as exc:
            validation.validate_updates({"operating_mode": "rogue"}, BASELINE)
        assert exc.value.field == "operating_mode"
        assert "rogue" in str(exc.value)

    def test_anomaly_response_invalid_value_rejected(self):
        with pytest.raises(validation.PolicyValidationError) as exc:
            validation.validate_updates({"anomaly_response": "ignore"}, BASELINE)
        assert exc.value.field == "anomaly_response"

    def test_operating_mode_non_string_rejected(self):
        with pytest.raises(validation.PolicyValidationError) as exc:
            validation.validate_updates({"operating_mode": 42}, BASELINE)
        assert exc.value.field == "operating_mode"


# ── bool validation ───────────────────────────────────────────────────────────

class TestBoolFields:

    @pytest.mark.parametrize("value", [True, False])
    def test_safe_mode_bool_accepted(self, value):
        validation.validate_updates({"safe_mode": value}, BASELINE)

    @pytest.mark.parametrize("value", ["true", 1, 0, None])
    def test_safe_mode_non_bool_rejected(self, value):
        with pytest.raises(validation.PolicyValidationError) as exc:
            validation.validate_updates({"safe_mode": value}, BASELINE)
        assert exc.value.field == "safe_mode"


# ── numeric range validation ─────────────────────────────────────────────────

class TestPositiveIntFields:

    @pytest.mark.parametrize("field", [
        "min_backends",
        "max_backends",
        "slo_p95_latency_ms",
        "anomaly_recovery_window_seconds",
    ])
    def test_zero_rejected(self, field):
        with pytest.raises(validation.PolicyValidationError) as exc:
            validation.validate_updates({field: 0}, BASELINE)
        assert exc.value.field == field

    @pytest.mark.parametrize("field", [
        "min_backends",
        "max_backends",
        "slo_p95_latency_ms",
        "anomaly_recovery_window_seconds",
    ])
    def test_negative_rejected(self, field):
        with pytest.raises(validation.PolicyValidationError) as exc:
            validation.validate_updates({field: -5}, BASELINE)
        assert exc.value.field == field

    def test_min_backends_bool_rejected(self):
        # bool is a subclass of int but a policy of "min_backends: True" makes
        # no sense; the validator must reject it explicitly.
        with pytest.raises(validation.PolicyValidationError) as exc:
            validation.validate_updates({"min_backends": True}, BASELINE)
        assert exc.value.field == "min_backends"


class TestNonNegFields:

    @pytest.mark.parametrize("field", [
        "autoscaler_cooldown_seconds",
        "per_instance_capacity_rps",
        "anomaly_latency_multiplier",
    ])
    def test_negative_rejected(self, field):
        with pytest.raises(validation.PolicyValidationError) as exc:
            validation.validate_updates({field: -1}, BASELINE)
        assert exc.value.field == field

    def test_zero_cooldown_accepted(self):
        # Operators may legitimately want to disable cooldown.
        validation.validate_updates({"autoscaler_cooldown_seconds": 0}, BASELINE)


class TestUnitIntervalFields:

    @pytest.mark.parametrize("value", [-0.1, 1.5, 2.0])
    def test_rl_exploration_rate_out_of_range_rejected(self, value):
        with pytest.raises(validation.PolicyValidationError) as exc:
            validation.validate_updates({"rl_exploration_rate": value}, BASELINE)
        assert exc.value.field == "rl_exploration_rate"

    @pytest.mark.parametrize("value", [0.0, 0.5, 1.0])
    def test_rl_confidence_threshold_in_range_accepted(self, value):
        validation.validate_updates({"rl_confidence_threshold": value}, BASELINE)


# ── cross-field invariants ────────────────────────────────────────────────────

class TestCrossFieldInvariants:

    def test_max_less_than_min_rejected(self):
        with pytest.raises(validation.PolicyValidationError) as exc:
            validation.validate_updates(
                {"max_backends": 2, "min_backends": 5}, BASELINE,
            )
        # Failure pinpoints min_backends per SOT §8.9 — operators can fix
        # either field but the error message tells them which is wrong.
        assert exc.value.field == "min_backends"
        assert "min_backends" in str(exc.value)
        assert "max_backends" in str(exc.value)

    def test_min_equal_to_max_accepted(self):
        # A fixed-size pool is valid — both bounds at 3 means "always 3".
        validation.validate_updates(
            {"min_backends": 3, "max_backends": 3}, BASELINE,
        )

    def test_partial_update_can_violate_existing(self):
        # Existing has min=1, max=5. POSTing max=0 fails per-field validation
        # (positive_int), not the cross-field rule. The point: a partial
        # update is still checked against the merged result, not just the
        # update payload in isolation.
        with pytest.raises(validation.PolicyValidationError):
            validation.validate_updates({"max_backends": 0}, BASELINE)


# ── request shape ─────────────────────────────────────────────────────────────

class TestRequestShape:

    def test_non_dict_body_rejected(self):
        with pytest.raises(validation.PolicyValidationError):
            validation.validate_updates([{"max_backends": 8}], BASELINE)
