"""
tests/unit/policy-manager/test_validation.py
────────────────────────────────────────────
Pure-Python unit tests for services/policy-manager/validation.py.

No Docker, no Redis, no DB — runs in the unit-tests CI job.

Coverage:
  1. Per-field shape checks (bool / int / float / enum / interval).
  2. Cross-field invariant: min_backends <= max_backends on the merged dict.
  3. Strict unknown-field rejection in `validate_updates` (#152) — POST
     bodies with keys outside the canonical schema raise PolicyValidationError
     and never leak to the merged dict.
  4. Server-managed echo strip — clients doing read-modify-write from a GET
     response may include `policy_version` / `timestamp` / `changed_fields`
     in the POST body; those are accepted but stripped before merge.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "policy-manager"
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from validation import (  # noqa: E402
    CANONICAL_POLICY_FIELDS,
    PolicyValidationError,
    validate_field,
    validate_merged_policy,
    validate_updates,
)


def _baseline_policy() -> dict:
    """A complete on-disk policy matching the canonical schema."""
    return {
        "operating_mode": "hybrid",
        "anomaly_response": "auto-isolate",
        "safe_mode": False,
        "min_backends": 1,
        "max_backends": 10,
        "slo_p95_latency_ms": 200,
        "anomaly_recovery_window_seconds": 30,
        "autoscaler_cooldown_seconds": 60,
        "per_instance_capacity_rps": 100,
        "anomaly_latency_multiplier": 3,
        "rl_exploration_rate": 0.0,
        "rl_confidence_threshold": 0.6,
    }


# ── per-field shape checks ───────────────────────────────────────────────────

class TestValidateField:
    def test_known_field_passes(self):
        validate_field("safe_mode", True)
        validate_field("min_backends", 3)
        validate_field("rl_confidence_threshold", 0.75)

    def test_known_field_rejects_wrong_type(self):
        with pytest.raises(PolicyValidationError) as exc:
            validate_field("safe_mode", "true")
        assert exc.value.field == "safe_mode"

    def test_unknown_field_is_noop_at_field_level(self):
        # validate_field is permissive on unknown keys — the strict gate
        # lives in validate_updates.
        validate_field("not_a_real_field", "anything")

    def test_enum_rejects_out_of_set(self):
        with pytest.raises(PolicyValidationError) as exc:
            validate_field("operating_mode", "all-rl")
        assert exc.value.field == "operating_mode"

    def test_unit_interval_rejects_out_of_range(self):
        with pytest.raises(PolicyValidationError):
            validate_field("rl_exploration_rate", 1.5)
        with pytest.raises(PolicyValidationError):
            validate_field("rl_confidence_threshold", -0.1)

    def test_positive_int_rejects_bool(self):
        # bool is a subclass of int in Python; the validator must reject it.
        with pytest.raises(PolicyValidationError):
            validate_field("min_backends", True)


# ── cross-field invariants ───────────────────────────────────────────────────

class TestMergedInvariants:
    def test_min_le_max_passes(self):
        validate_merged_policy({"min_backends": 1, "max_backends": 10})

    def test_min_gt_max_fails(self):
        with pytest.raises(PolicyValidationError) as exc:
            validate_merged_policy({"min_backends": 5, "max_backends": 3})
        assert exc.value.field == "min_backends"


# ── #152: strict unknown-field rejection ─────────────────────────────────────

class TestUnknownFieldsRejected:
    """The bug repro: POST body containing `actor` (a header-only field) must
    be rejected with 400, never persisted to config/policy.yaml."""

    def test_actor_in_body_is_rejected(self):
        with pytest.raises(PolicyValidationError) as exc:
            validate_updates(
                {"rl_confidence_threshold": 0.9, "actor": "lb-sidecar-review"},
                _baseline_policy(),
            )
        assert exc.value.field == "actor"
        assert "actor" in str(exc.value)

    def test_typo_field_is_rejected(self):
        with pytest.raises(PolicyValidationError) as exc:
            validate_updates(
                {"max_backendss": 5},  # typo
                _baseline_policy(),
            )
        assert exc.value.field == "max_backendss"

    def test_multiple_unknown_fields_reports_sorted_set(self):
        with pytest.raises(PolicyValidationError) as exc:
            validate_updates(
                {"actor": "x", "zzz": 1, "aaa": 2},
                _baseline_policy(),
            )
        # The error message lists all unknowns sorted; the .field attribute
        # points at the first alphabetically.
        assert exc.value.field == "aaa"
        msg = str(exc.value)
        assert "aaa" in msg and "actor" in msg and "zzz" in msg

    def test_unknown_field_blocks_persistence(self):
        # The merged dict is never returned when validation fails — caller
        # cannot accidentally persist the unknown key.
        existing = _baseline_policy()
        try:
            validate_updates({"safe_mode": True, "actor": "ops"}, existing)
        except PolicyValidationError:
            pass
        # `existing` was not mutated.
        assert "actor" not in existing


# ── server-managed echo strip ────────────────────────────────────────────────

class TestServerManagedEchoStrip:
    """Clients doing GET → modify → POST may echo back server-set fields
    (policy_version, timestamp, changed_fields). Those must be accepted
    without 400 but stripped from the merge result."""

    def test_policy_version_echo_is_stripped(self):
        existing = {**_baseline_policy(), "policy_version": 29}
        merged = validate_updates(
            {"policy_version": 42, "safe_mode": True},
            existing,
        )
        # The user-settable field landed in the merge ...
        assert merged["safe_mode"] is True
        # ... and policy_version came from existing, not the body. The server
        # rewrites it again after this anyway (`new_version` in app.py).
        assert merged["policy_version"] == 29

    def test_timestamp_echo_is_stripped(self):
        existing = _baseline_policy()
        merged = validate_updates(
            {"timestamp": "2026-06-03T12:00:00Z", "max_backends": 8},
            existing,
        )
        assert merged["max_backends"] == 8
        assert "timestamp" not in merged

    def test_changed_fields_echo_is_stripped(self):
        existing = _baseline_policy()
        merged = validate_updates(
            {"changed_fields": ["safe_mode"], "safe_mode": True},
            existing,
        )
        assert merged["safe_mode"] is True
        assert "changed_fields" not in merged

    def test_only_server_managed_fields_is_a_noop_merge(self):
        # A body containing only echoed envelope metadata is valid; the
        # merge equals `existing` (no user-settable changes).
        existing = _baseline_policy()
        merged = validate_updates(
            {"policy_version": 99, "timestamp": "2026-06-03T12:00:00Z"},
            existing,
        )
        assert merged == existing


# ── happy paths (regression guards) ──────────────────────────────────────────

class TestHappyPath:
    def test_full_canonical_post_passes(self):
        existing = _baseline_policy()
        new = {k: existing[k] for k in CANONICAL_POLICY_FIELDS}
        new["rl_confidence_threshold"] = 0.85
        merged = validate_updates(new, existing)
        assert merged["rl_confidence_threshold"] == 0.85

    def test_partial_post_keeps_existing(self):
        existing = _baseline_policy()
        merged = validate_updates({"safe_mode": True}, existing)
        assert merged["safe_mode"] is True
        # untouched field survives the merge
        assert merged["max_backends"] == existing["max_backends"]

    def test_empty_body_returns_existing(self):
        existing = _baseline_policy()
        assert validate_updates({}, existing) == existing

    def test_non_dict_body_rejected(self):
        with pytest.raises(PolicyValidationError):
            validate_updates([1, 2, 3], _baseline_policy())

    def test_canonical_field_set_matches_field_checks_keys(self):
        # Guard against the canonical set drifting from the per-field rules.
        from validation import _FIELD_CHECKS
        assert CANONICAL_POLICY_FIELDS == frozenset(_FIELD_CHECKS.keys())
