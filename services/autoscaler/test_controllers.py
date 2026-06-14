"""Unit tests for the target-based scaling controllers.

These exercise ``target_for_load`` (the two sizing laws) and ``decide_target``
(multi-step scale-out, asymmetric cooldowns, scale-in deadband and bounds).
Everything is a pure function of its inputs, so the tests are deterministic —
no clock, no randomness, no I/O.
"""

import sys
from pathlib import Path

# Put the autoscaler service dir on sys.path so the flat ``import controllers``
# / ``import decisions`` (the same layout app.py uses) resolves standalone.
_SERVICE_DIR = Path(__file__).resolve().parent
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

from controllers import ControlPolicy, decide_target, target_for_load  # noqa: E402
from decisions import (  # noqa: E402
    ACTION_NOOP,
    ACTION_SCALE_IN,
    ACTION_SCALE_OUT,
)


def _headroom_policy(**overrides):
    base = dict(
        min_backends=1,
        max_backends=50,
        per_instance_capacity_rps=100.0,
        headroom=0.15,
        sizing="headroom",
        scale_out_cooldown_s=0.0,
        scale_in_cooldown_s=120.0,
        max_step_out=0,
        max_step_in=1,
        scale_in_deadband=0.15,
    )
    base.update(overrides)
    return ControlPolicy(**base)


# ── target_for_load: headroom sizing ────────────────────────────────────────


def test_headroom_sizing_applies_ceil_and_margin():
    # ceil(800 * 1.15 / 100) = ceil(9.2) = 10
    policy = _headroom_policy()
    assert target_for_load(800.0, policy) == 10


def test_headroom_sizing_clamps_to_max():
    # ceil(10000 * 1.15 / 100) = 115, clamped to max_backends=50.
    policy = _headroom_policy(max_backends=50)
    assert target_for_load(10000.0, policy) == 50


def test_headroom_sizing_clamps_to_min():
    # ceil(10 * 1.15 / 100) = ceil(0.115) = 1, but min_backends=3 floors it.
    policy = _headroom_policy(min_backends=3)
    assert target_for_load(10.0, policy) == 3


def test_headroom_sizing_monotonic_non_decreasing():
    policy = _headroom_policy(min_backends=1, max_backends=200)
    prev = -1
    for load in range(0, 20001, 137):
        cur = target_for_load(float(load), policy)
        assert cur >= prev
        prev = cur


# ── target_for_load: sqrt_staffing sizing ───────────────────────────────────


def test_sqrt_staffing_concrete_value():
    # a = 400/100 = 4; raw = 4 + 1*sqrt(4) = 6; ceil = 6.
    policy = _headroom_policy(sizing="sqrt_staffing", qos_beta=1.0, max_backends=100)
    assert target_for_load(400.0, policy) == 6


def test_sqrt_staffing_higher_beta_increases_target():
    # Same offered load, larger beta must demand at least as many instances,
    # and strictly more here: beta=2 -> 4 + 2*2 = 8 > 6.
    low = _headroom_policy(sizing="sqrt_staffing", qos_beta=1.0, max_backends=100)
    high = _headroom_policy(sizing="sqrt_staffing", qos_beta=2.0, max_backends=100)
    assert target_for_load(400.0, high) > target_for_load(400.0, low)
    assert target_for_load(400.0, high) == 8


# ── target_for_load: invalid capacity ───────────────────────────────────────


def test_non_positive_capacity_returns_min_backends():
    policy = _headroom_policy(min_backends=4, per_instance_capacity_rps=0.0)
    assert target_for_load(5000.0, policy) == 4
    neg = _headroom_policy(min_backends=4, per_instance_capacity_rps=-10.0)
    assert target_for_load(5000.0, neg) == 4


# ── decide_target: multi-step scale-out ─────────────────────────────────────


def test_scale_out_jumps_multiple_steps_when_unbounded():
    # Sized target = 10 (see headroom test); from 2 backends with max_step_out=0
    # the controller jumps straight to 10 in a single action.
    policy = _headroom_policy(max_step_out=0)
    d = decide_target(
        predicted_rps=800.0,
        current_count=2,
        policy=policy,
        seconds_since_scale_out=None,
        seconds_since_scale_in=None,
    )
    assert d.action == ACTION_SCALE_OUT
    assert d.target_count == 10


def test_max_step_out_caps_the_jump():
    policy = _headroom_policy(max_step_out=3)
    d = decide_target(
        predicted_rps=800.0,
        current_count=2,
        policy=policy,
        seconds_since_scale_out=None,
        seconds_since_scale_in=None,
    )
    assert d.action == ACTION_SCALE_OUT
    # target is 10 but step is capped at +3 -> 5.
    assert d.target_count == 5


# ── decide_target: asymmetric cooldowns ─────────────────────────────────────


def test_scale_out_blocked_during_scale_out_cooldown():
    policy = _headroom_policy(scale_out_cooldown_s=60.0)
    d = decide_target(
        predicted_rps=800.0,
        current_count=2,
        policy=policy,
        seconds_since_scale_out=30.0,
        seconds_since_scale_in=None,
    )
    assert d.action == ACTION_NOOP
    assert d.target_count == 2


def test_recent_scale_in_does_not_block_scale_out():
    # Independent timers: a fresh scale-in (1s ago) must not gate an urgent
    # scale-out, even though scale_in_cooldown is long.
    policy = _headroom_policy(scale_out_cooldown_s=60.0, scale_in_cooldown_s=600.0)
    d = decide_target(
        predicted_rps=800.0,
        current_count=2,
        policy=policy,
        seconds_since_scale_out=None,
        seconds_since_scale_in=1.0,
    )
    assert d.action == ACTION_SCALE_OUT
    assert d.target_count == 10


def test_scale_in_blocked_during_scale_in_cooldown():
    # target = ceil(50 * 1.15 / 100) = 1; from 10 backends a scale-in is wanted,
    # the deadband is satisfied (9 * 100 = 900 >= 50*1.3), but the scale-in
    # cooldown holds it.
    policy = _headroom_policy(scale_in_cooldown_s=120.0)
    d = decide_target(
        predicted_rps=50.0,
        current_count=10,
        policy=policy,
        seconds_since_scale_out=None,
        seconds_since_scale_in=30.0,
    )
    assert d.action == ACTION_NOOP
    assert d.target_count == 10


def test_recent_scale_out_does_not_block_scale_in():
    # A fresh scale-out must not gate a scale-in (independent timers).
    policy = _headroom_policy(scale_out_cooldown_s=600.0, scale_in_cooldown_s=120.0)
    d = decide_target(
        predicted_rps=50.0,
        current_count=10,
        policy=policy,
        seconds_since_scale_out=1.0,
        seconds_since_scale_in=None,
    )
    assert d.action == ACTION_SCALE_IN
    assert d.target_count == 9


def test_none_timers_never_block():
    # seconds_since_* = None means "never acted" -> cooldown never applies.
    policy = _headroom_policy(scale_out_cooldown_s=600.0, scale_in_cooldown_s=600.0)
    d = decide_target(
        predicted_rps=800.0,
        current_count=2,
        policy=policy,
        seconds_since_scale_out=None,
        seconds_since_scale_in=None,
    )
    assert d.action == ACTION_SCALE_OUT


# ── decide_target: scale-in step cap ────────────────────────────────────────


def test_max_step_in_caps_shed_to_one_by_default():
    # target = 1, from 10, deadband satisfied, no cooldown -> sheds only 1.
    policy = _headroom_policy(max_step_in=1, scale_in_cooldown_s=0.0)
    d = decide_target(
        predicted_rps=50.0,
        current_count=10,
        policy=policy,
        seconds_since_scale_out=None,
        seconds_since_scale_in=None,
    )
    assert d.action == ACTION_SCALE_IN
    assert d.target_count == 9


def test_max_step_in_can_shed_several():
    policy = _headroom_policy(max_step_in=4, scale_in_cooldown_s=0.0)
    d = decide_target(
        predicted_rps=50.0,
        current_count=10,
        policy=policy,
        seconds_since_scale_out=None,
        seconds_since_scale_in=None,
    )
    assert d.action == ACTION_SCALE_IN
    assert d.target_count == 6


# ── decide_target: scale-in deadband / hysteresis ───────────────────────────


def test_scale_in_deadband_holds_near_boundary():
    # current=4 backends, cap=100 -> 400 rps capacity. target sizing wants 3
    # (ceil(300*1.15/100)=ceil(3.45)=4? no: pick a load just under boundary).
    # Choose load where target < current but shedding one breaches the band:
    # load=300, headroom=0.15, deadband=0.15 -> shed_floor = 300*1.30 = 390.
    # post-shed capacity = (4-1)*100 = 300 < 390 -> hold.
    # And target = ceil(300*1.15/100) = ceil(3.45) = 4 == current... so use a
    # load whose sized target is below current yet inside the deadband.
    policy = _headroom_policy(scale_in_deadband=0.15, scale_in_cooldown_s=0.0)
    # load=250: target = ceil(250*1.15/100) = ceil(2.875) = 3 < current(4).
    # shed_floor = 250 * 1.30 = 325; post-shed cap = 3*100 = 300 < 325 -> hold.
    d = decide_target(
        predicted_rps=250.0,
        current_count=4,
        policy=policy,
        seconds_since_scale_out=None,
        seconds_since_scale_in=None,
    )
    assert d.action == ACTION_NOOP
    assert d.target_count == 4


def test_scale_in_proceeds_when_clear_of_deadband():
    # Same shape, but load well below the band so shedding is safe.
    # load=150: target = ceil(150*1.15/100) = ceil(1.725) = 2 < current(4).
    # shed_floor = 150 * 1.30 = 195; post-shed cap = 3*100 = 300 >= 195 -> shed.
    policy = _headroom_policy(scale_in_deadband=0.15, scale_in_cooldown_s=0.0, max_step_in=1)
    d = decide_target(
        predicted_rps=150.0,
        current_count=4,
        policy=policy,
        seconds_since_scale_out=None,
        seconds_since_scale_in=None,
    )
    assert d.action == ACTION_SCALE_IN
    assert d.target_count == 3


# ── decide_target: bounds and noop ──────────────────────────────────────────


def test_never_exceeds_max_backends():
    policy = _headroom_policy(min_backends=1, max_backends=8, max_step_out=0)
    d = decide_target(
        predicted_rps=100000.0,
        current_count=2,
        policy=policy,
        seconds_since_scale_out=None,
        seconds_since_scale_in=None,
    )
    assert d.action == ACTION_SCALE_OUT
    assert d.target_count == 8


def test_never_below_min_backends():
    policy = _headroom_policy(min_backends=3, max_backends=50,
                              max_step_in=100, scale_in_cooldown_s=0.0)
    d = decide_target(
        predicted_rps=10.0,
        current_count=5,
        policy=policy,
        seconds_since_scale_out=None,
        seconds_since_scale_in=None,
    )
    assert d.action == ACTION_SCALE_IN
    assert d.target_count == 3


def test_noop_when_target_equals_current():
    # load=800 sizes to 10; current already 10 -> hold.
    policy = _headroom_policy()
    d = decide_target(
        predicted_rps=800.0,
        current_count=10,
        policy=policy,
        seconds_since_scale_out=None,
        seconds_since_scale_in=None,
    )
    assert d.action == ACTION_NOOP
    assert d.target_count == 10


def test_non_positive_capacity_refuses_to_scale():
    policy = _headroom_policy(per_instance_capacity_rps=0.0)
    d = decide_target(
        predicted_rps=5000.0,
        current_count=4,
        policy=policy,
        seconds_since_scale_out=None,
        seconds_since_scale_in=None,
    )
    assert d.action == ACTION_NOOP
    assert d.target_count == 4
