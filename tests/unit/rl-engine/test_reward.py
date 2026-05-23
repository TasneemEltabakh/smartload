"""
tests/unit/rl-engine/test_reward.py
────────────────────────────────────
Unit tests for services/rl-engine/training/reward.py.

No Docker, no Redis, no DB.

Coverage:
  1. compute() returns a float
  2. CRITICAL (Amendment F): modifying next_state changes the reward;
     modifying state alone does NOT change the reward
  3. Load-imbalance penalty grows with std(request_counts)
  4. Hard penalty applied when chosen backend is unhealthy
  5. Action out of range returns unhealthy penalty
  6. Empty next_state returns 0.0
  7. Higher latency in next_state gives lower reward
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "rl-engine"
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from obs_builder import NormParams          # noqa: E402
from policy_base import BackendState        # noqa: E402
from training.reward import RewardCalculator  # noqa: E402

_NORM = NormParams(latency_scale=500.0, request_count_scale=100.0)
_CALC = RewardCalculator(norm=_NORM, imbalance_lambda=0.1)


def _b(bid: str, latency: float, qd: int = 10, health: str = "healthy") -> BackendState:
    return BackendState(backend_id=bid, latency_ms=latency,
                        queue_depth=qd, health=health)


def _state(latencies: list[float], health: str = "healthy") -> list[BackendState]:
    return [_b(f"b{i}", lat, health=health) for i, lat in enumerate(latencies)]


# ── return type ───────────────────────────────────────────────────────────────

def test_compute_returns_float():
    state = _state([100.0, 200.0])
    r = _CALC.compute(state, action=0, next_state=state)
    assert isinstance(r, float)


# ── Amendment F — credit-assignment correctness ───────────────────────────────

def test_modifying_next_state_changes_reward():
    """Changing the chosen backend's latency in next_state must change reward."""
    pre  = _state([100.0, 100.0])
    low  = _state([100.0, 100.0])   # chosen b0 latency = 100
    high = _state([400.0, 100.0])   # chosen b0 latency = 400

    reward_low  = _CALC.compute(pre, action=0, next_state=low)
    reward_high = _CALC.compute(pre, action=0, next_state=high)
    assert reward_low > reward_high, (
        "Higher latency in next_state must yield lower reward. "
        "latency_ms_chosen must be read from next_state, not state."
    )


def test_modifying_state_alone_does_not_change_reward():
    """Changing the pre-action state's latency must NOT affect the reward
    (the reward only depends on next_state for the latency term)."""
    next_state = _state([100.0, 100.0])

    pre_low  = _state([50.0,  50.0])   # low latency before action
    pre_high = _state([450.0, 450.0])  # high latency before action

    reward_low  = _CALC.compute(pre_low,  action=0, next_state=next_state)
    reward_high = _CALC.compute(pre_high, action=0, next_state=next_state)
    assert reward_low == pytest.approx(reward_high), (
        "Changing state (pre-action) while keeping next_state identical "
        "must not change the reward. Credit assignment is from next_state only."
    )


# ── imbalance penalty ─────────────────────────────────────────────────────────

def test_imbalance_penalty_grows_with_load_variance():
    """Higher std(request_counts) across backends → lower (more negative) reward."""
    balanced   = [_b("b0", 100.0, qd=10), _b("b1", 100.0, qd=10)]
    imbalanced = [_b("b0", 100.0, qd=1),  _b("b1", 100.0, qd=100)]

    pre = balanced
    r_balanced   = _CALC.compute(pre, action=0, next_state=balanced)
    r_imbalanced = _CALC.compute(pre, action=0, next_state=imbalanced)
    assert r_balanced > r_imbalanced


def test_perfectly_balanced_pool_has_zero_imbalance_contribution():
    """Equal queue_depth across all backends → imbalance term is 0."""
    state = [_b(f"b{i}", 100.0, qd=10) for i in range(5)]
    # std of [10, 10, 10, 10, 10] = 0 → imbalance = 0 / (10 + 1) = 0
    import numpy as np
    counts = [10] * 5
    expected_imbalance = float(np.std(counts) / (np.mean(counts) + 1.0))
    assert expected_imbalance == pytest.approx(0.0)

    r = _CALC.compute(state, action=0, next_state=state)
    # Reward should be just the latency term
    expected = -(100.0 / 500.0)
    assert r == pytest.approx(expected, abs=1e-4)


# ── unhealthy penalty ─────────────────────────────────────────────────────────

def test_unhealthy_chosen_backend_incurs_hard_penalty():
    """Routing to an unhealthy backend must add the -10.0 penalty."""
    next_healthy   = _state([100.0])
    next_unhealthy = [_b("b0", 100.0, health="unhealthy")]

    r_h = _CALC.compute(next_healthy, action=0, next_state=next_healthy)
    r_u = _CALC.compute(next_healthy, action=0, next_state=next_unhealthy)
    assert r_u < r_h
    assert r_u == pytest.approx(r_h - 10.0, abs=1e-3)


def test_degraded_backend_has_no_hard_penalty():
    """Degraded backends are slow but not penalised extra; only the latency
    term reflects their cost."""
    next_degraded = [_b("b0", 100.0, health="degraded")]
    next_healthy  = [_b("b0", 100.0, health="healthy")]
    # Same latency, no hard penalty → same reward
    r_d = _CALC.compute(next_degraded, action=0, next_state=next_degraded)
    r_h = _CALC.compute(next_healthy,  action=0, next_state=next_healthy)
    assert r_d == pytest.approx(r_h, abs=1e-4)


# ── edge cases ────────────────────────────────────────────────────────────────

def test_empty_next_state_returns_zero():
    state = _state([100.0])
    r = _CALC.compute(state, action=0, next_state=[])
    assert r == pytest.approx(0.0)


def test_action_out_of_range_returns_unhealthy_penalty():
    state = _state([100.0])
    r = _CALC.compute(state, action=99, next_state=state)
    assert r == pytest.approx(-10.0)


def test_higher_latency_gives_lower_reward():
    pre = _state([100.0])
    low  = [_b("b0", 50.0)]
    high = [_b("b0", 400.0)]
    assert _CALC.compute(pre, 0, low) > _CALC.compute(pre, 0, high)
