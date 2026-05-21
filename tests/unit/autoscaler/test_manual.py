"""
tests/unit/autoscaler/test_manual.py
─────────────────────────────────────
Pure-Python unit tests for services/autoscaler/manual.py.

No Docker, no DB, no Redis — runs in the unit-tests CI job.

Coverage:
  1. Validation:
     - non-integer target_count          → ManualScaleError(field='target_count')
     - negative target_count             → ManualScaleError
     - target below policy.min_backends  → ManualScaleError
     - target above policy.max_backends  → ManualScaleError
  2. Direction:
     - target > current → SCALE_OUT with correct step count
     - target < current → SCALE_IN with correct step count
     - target == current → NOOP
  3. Reason composition:
     - prefix `manual:<actor>:` always present
     - empty / whitespace actor → falls back to 'operator'
     - empty / missing user_reason → falls back to 'manual override'
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "autoscaler"
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from decisions import ACTION_NOOP, ACTION_SCALE_IN, ACTION_SCALE_OUT, Policy  # noqa: E402
from manual import ManualScaleError, plan_manual_scale                       # noqa: E402


_POLICY = Policy(
    min_backends=1,
    max_backends=5,
    per_instance_capacity_rps=100.0,
    cooldown_seconds=60.0,
)


def _plan(target, current=3, actor="op", reason="manual"):
    return plan_manual_scale(
        target_count=target,
        current_count=current,
        policy=_POLICY,
        actor=actor,
        user_reason=reason,
    )


# ── validation ───────────────────────────────────────────────────────────────

class TestValidation:

    def test_non_integer_target_raises(self):
        with pytest.raises(ManualScaleError) as exc:
            _plan(target="not-a-number")
        assert exc.value.field == "target_count"

    def test_float_target_is_coerced(self):
        """int('3') and int(3.0) both succeed — that's fine; we want 3."""
        plan = _plan(target=3.0, current=3)
        assert plan.action == ACTION_NOOP
        assert plan.target_count == 3

    def test_negative_target_raises(self):
        with pytest.raises(ManualScaleError) as exc:
            _plan(target=-1)
        assert exc.value.field == "target_count"

    def test_target_below_min_raises(self):
        with pytest.raises(ManualScaleError) as exc:
            _plan(target=0)
        assert exc.value.field == "target_count"
        assert "min_backends" in exc.value.message

    def test_target_above_max_raises(self):
        with pytest.raises(ManualScaleError) as exc:
            _plan(target=6)
        assert exc.value.field == "target_count"
        assert "max_backends" in exc.value.message

    def test_target_exactly_min_is_accepted(self):
        plan = _plan(target=1, current=3)
        assert plan.action == ACTION_SCALE_IN
        assert plan.steps == 2

    def test_target_exactly_max_is_accepted(self):
        plan = _plan(target=5, current=3)
        assert plan.action == ACTION_SCALE_OUT
        assert plan.steps == 2


# ── direction ────────────────────────────────────────────────────────────────

class TestDirection:

    def test_target_greater_than_current_scales_out(self):
        plan = _plan(target=5, current=2)
        assert plan.action == ACTION_SCALE_OUT
        assert plan.steps == 3
        assert plan.target_count == 5

    def test_target_less_than_current_scales_in(self):
        plan = _plan(target=2, current=5)
        assert plan.action == ACTION_SCALE_IN
        assert plan.steps == 3
        assert plan.target_count == 2

    def test_target_equal_to_current_is_noop(self):
        plan = _plan(target=3, current=3)
        assert plan.action == ACTION_NOOP
        assert plan.steps == 0
        assert plan.target_count == 3


# ── reason composition ──────────────────────────────────────────────────────

class TestReason:

    def test_reason_prefixed_with_actor(self):
        plan = _plan(target=4, actor="alice", reason="failover drill")
        assert plan.reason == "manual:alice: failover drill"

    def test_missing_actor_falls_back_to_operator(self):
        plan = _plan(target=4, actor="", reason="r")
        assert plan.reason.startswith("manual:operator:")

    def test_whitespace_actor_falls_back_to_operator(self):
        plan = _plan(target=4, actor="   ", reason="r")
        assert plan.reason.startswith("manual:operator:")

    def test_missing_user_reason_falls_back_to_default(self):
        plan = _plan(target=4, actor="bob", reason=None)
        assert plan.reason == "manual:bob: manual override"

    def test_empty_user_reason_falls_back_to_default(self):
        plan = _plan(target=4, actor="bob", reason="   ")
        assert plan.reason == "manual:bob: manual override"
