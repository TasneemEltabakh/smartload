"""
tests/unit/autoscaler/test_controller_wiring.py
────────────────────────────────────────────────
Unit tests for the pure glue that wires the target-based controllers
(controllers.py) into the autoscaler control loop (app.py):

  1. control_policy_from — projects the live Policy bounds onto a ControlPolicy
     and layers the deploy-time tuning on top.
  2. select_decision     — dispatches to decide() under "step" and
     decide_target() under "target", reading the right cooldown clocks.
  3. actuate_to_target   — drives current→target one instance at a time, stops
     at the target or an exhausted pool, and reports the count actually reached.

These are the maths app.py delegates to; keeping them pure means this suite has
no Docker / DB / Redis / Prometheus dependency and imports only controllers +
decisions (the same modules app.py imports).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Autoscaler service dir at the FRONT of sys.path; purge sibling-service modules
# of the same basename a prior test may have cached under pytest prepend mode.
_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "autoscaler"
for _name in ("controllers", "decisions", "app", "manual", "cluster_client"):
    sys.modules.pop(_name, None)
sys.path.insert(0, str(_SERVICE))

from controllers import (  # noqa: E402
    ControlPolicy,
    actuate_to_target,
    control_policy_from,
    select_decision,
)
from decisions import (  # noqa: E402
    ACTION_NOOP,
    ACTION_SCALE_IN,
    ACTION_SCALE_OUT,
    Policy,
)


_POLICY = Policy(min_backends=1, max_backends=50, per_instance_capacity_rps=100.0,
                 cooldown_seconds=60.0)

_TUNING = dict(
    headroom=0.15,
    sizing="headroom",
    qos_beta=1.0,
    scale_out_cooldown_s=0.0,
    scale_in_cooldown_s=120.0,
    max_step_out=0,
    max_step_in=1,
    scale_in_deadband=0.15,
)


def _decide(kind, **overrides):
    """select_decision with sensible defaults; overrides win."""
    tuning = {**_TUNING, **{k: overrides.pop(k) for k in list(overrides)
                            if k in _TUNING}}
    kw = dict(
        predicted_rps=800.0,
        current_count=2,
        step_policy=_POLICY,
        control_policy=control_policy_from(_POLICY, **tuning),
        seconds_since_last_action=None,
        seconds_since_scale_out=None,
        seconds_since_scale_in=None,
        now_text="forecast",
    )
    kw.update(overrides)
    return select_decision(kind, **kw)


# ── control_policy_from ─────────────────────────────────────────────────────

def test_control_policy_from_maps_live_bounds_and_tuning():
    cp = control_policy_from(
        Policy(2, 8, 100.0, 60.0),
        headroom=0.2, sizing="sqrt_staffing", qos_beta=1.5,
        scale_out_cooldown_s=10.0, scale_in_cooldown_s=200.0,
        max_step_out=4, max_step_in=2, scale_in_deadband=0.25,
    )
    assert isinstance(cp, ControlPolicy)
    assert (cp.min_backends, cp.max_backends, cp.per_instance_capacity_rps) == (2, 8, 100.0)
    assert cp.headroom == 0.2
    assert cp.sizing == "sqrt_staffing"
    assert cp.qos_beta == 1.5
    assert cp.scale_out_cooldown_s == 10.0
    assert cp.scale_in_cooldown_s == 200.0
    assert cp.max_step_out == 4
    assert cp.max_step_in == 2
    assert cp.scale_in_deadband == 0.25


# ── select_decision dispatch ────────────────────────────────────────────────

def test_step_controller_moves_one_instance():
    # capacity = 2 * 100 = 200; 250 > 200 → scale out by exactly one.
    d = _decide("step", predicted_rps=250.0, current_count=2)
    assert d.action == ACTION_SCALE_OUT
    assert d.target_count == 3


def test_unknown_kind_falls_through_to_step():
    # Any non-"target" kind routes to decide(); the same single-step result.
    d = _decide("anything-else", predicted_rps=250.0, current_count=2)
    assert d.action == ACTION_SCALE_OUT
    assert d.target_count == 3


def test_target_controller_jumps_multiple_instances():
    # target = ceil(800 * 1.15 / 100) = 10; unbounded step jumps straight there.
    d = _decide("target", predicted_rps=800.0, current_count=2, max_step_out=0)
    assert d.action == ACTION_SCALE_OUT
    assert d.target_count == 10


def test_target_controller_recent_scale_in_does_not_block_scale_out():
    # A scale-in 1s ago must NOT gate an urgent scale-out (independent clocks).
    d = _decide(
        "target", predicted_rps=800.0, current_count=2,
        scale_out_cooldown_s=60.0, scale_in_cooldown_s=600.0,
        seconds_since_scale_out=None, seconds_since_scale_in=1.0,
    )
    assert d.action == ACTION_SCALE_OUT


def test_target_controller_scale_out_cooldown_blocks():
    d = _decide(
        "target", predicted_rps=800.0, current_count=2,
        scale_out_cooldown_s=60.0, seconds_since_scale_out=30.0,
    )
    assert d.action == ACTION_NOOP
    assert d.target_count == 2


def test_step_controller_uses_single_action_clock():
    # The step controller ignores the per-direction clocks and honours the
    # single action clock: a recent action within cooldown suppresses.
    d = _decide(
        "step", predicted_rps=250.0, current_count=2,
        seconds_since_last_action=10.0,   # < 60s cooldown
    )
    assert d.action == ACTION_NOOP


# ── actuate_to_target ───────────────────────────────────────────────────────

class _Counter:
    """A scale_fn that actuates up to `limit` times, then returns None."""

    def __init__(self, limit: int | None = None, mechanism: str = "start"):
        self.calls = 0
        self._limit = limit
        self._mechanism = mechanism

    def __call__(self):
        self.calls += 1
        if self._limit is not None and self.calls > self._limit:
            return None
        return ("fake-backend", self._mechanism)


def test_actuate_scale_out_full_jump():
    fn = _Counter()
    actuated, final_count, name, mech = actuate_to_target(ACTION_SCALE_OUT, 2, 10, fn)
    assert actuated == 8
    assert final_count == 10
    assert fn.calls == 8
    assert (name, mech) == ("fake-backend", "start")


def test_actuate_scale_in_full_drain():
    fn = _Counter()
    actuated, final_count, _, _ = actuate_to_target(ACTION_SCALE_IN, 6, 4, fn)
    assert actuated == 2
    assert final_count == 4
    assert fn.calls == 2


def test_actuate_partial_when_pool_exhausted():
    # Pool can only actuate 3 of the 8 requested steps.
    fn = _Counter(limit=3)
    actuated, final_count, _, _ = actuate_to_target(ACTION_SCALE_OUT, 2, 10, fn)
    assert actuated == 3
    assert final_count == 5            # 2 + 3 actually reached, not 10
    assert fn.calls == 4               # 4th call returned None and stopped


def test_actuate_zero_when_first_step_fails():
    fn = _Counter(limit=0)
    actuated, final_count, name, mech = actuate_to_target(ACTION_SCALE_OUT, 5, 8, fn)
    assert actuated == 0
    assert final_count == 5
    assert (name, mech) == (None, None)


def test_actuate_single_step_matches_step_controller():
    fn = _Counter()
    actuated, final_count, _, _ = actuate_to_target(ACTION_SCALE_OUT, 3, 4, fn)
    assert actuated == 1
    assert final_count == 4
    assert fn.calls == 1


def test_actuate_noop_does_nothing():
    fn = _Counter()
    actuated, final_count, name, mech = actuate_to_target(ACTION_NOOP, 4, 4, fn)
    assert actuated == 0
    assert final_count == 4
    assert fn.calls == 0
    assert (name, mech) == (None, None)
