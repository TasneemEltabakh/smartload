"""
tests/unit/rl-engine/test_obs_builder.py
─────────────────────────────────────────
Unit tests for services/rl-engine/obs_builder.py.

No Docker, no Redis, no DB.

Coverage:
  1. build_observation — shape, dtype, correct values, padding, sort order
  2. build_action_mask — all-healthy, mixed, all-unhealthy, empty state
  3. all_masked_fallback — exactly one True; picks lowest latency; empty state
  4. NormParams — JSON round-trip, from_dict type coercion
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "rl-engine"
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from obs_builder import (       # noqa: E402
    N_MAX_BACKENDS,
    NormParams,
    all_masked_fallback,
    build_action_mask,
    build_observation,
)
from policy_base import BackendState  # noqa: E402

_NORM = NormParams(latency_scale=500.0, request_count_scale=100.0)


def _b(backend_id: str, latency_ms: float, queue_depth: int, health: str) -> BackendState:
    return BackendState(backend_id=backend_id, latency_ms=latency_ms,
                        queue_depth=queue_depth, health=health)


# ── build_observation ─────────────────────────────────────────────────────────

def test_obs_shape_and_dtype():
    state = [_b("b1", 100.0, 10, "healthy")]
    obs = build_observation(state, N_MAX_BACKENDS, _NORM)
    assert obs.shape == (N_MAX_BACKENDS * 3,)
    assert obs.dtype == np.float32


def test_obs_single_backend_values():
    state = [_b("b1", 250.0, 50, "healthy")]
    obs = build_observation(state, N_MAX_BACKENDS, _NORM)
    assert obs[0] == pytest.approx(250.0 / 500.0)    # latency_norm
    assert obs[1] == pytest.approx(50.0  / 100.0)    # request_count_norm
    assert obs[2] == pytest.approx(0.0)               # health_flag healthy


def test_obs_health_flags():
    state = [
        _b("a", 50.0, 5, "healthy"),
        _b("b", 50.0, 5, "degraded"),
        _b("c", 50.0, 5, "unhealthy"),
    ]
    obs = build_observation(state, N_MAX_BACKENDS, _NORM)
    assert obs[2]  == pytest.approx(0.0)   # healthy
    assert obs[5]  == pytest.approx(0.5)   # degraded
    assert obs[8]  == pytest.approx(1.0)   # unhealthy


def test_obs_padding_slots_are_zero_load_unhealthy():
    """Absent backends must appear as [0.0, 0.0, 1.0]."""
    state = [_b("b1", 100.0, 10, "healthy")]
    obs = build_observation(state, N_MAX_BACKENDS, _NORM)
    for i in range(1, N_MAX_BACKENDS):
        base = i * 3
        assert obs[base]     == pytest.approx(0.0), f"slot {i} latency_norm != 0"
        assert obs[base + 1] == pytest.approx(0.0), f"slot {i} req_count_norm != 0"
        assert obs[base + 2] == pytest.approx(1.0), f"slot {i} health_flag != 1.0"


def test_obs_sorted_by_backend_id():
    """Backends must be sorted lexicographically so slot assignment is stable."""
    state = [
        _b("z_last",  200.0, 20, "healthy"),
        _b("a_first", 100.0, 10, "healthy"),
    ]
    obs = build_observation(state, N_MAX_BACKENDS, _NORM)
    # Slot 0 → "a_first" (latency=100 → norm=0.2)
    # Slot 1 → "z_last"  (latency=200 → norm=0.4)
    assert obs[0] == pytest.approx(100.0 / 500.0)
    assert obs[3] == pytest.approx(200.0 / 500.0)


def test_obs_full_pool_no_padding():
    state = [_b(f"b{i}", float(i * 10), i, "healthy") for i in range(1, N_MAX_BACKENDS + 1)]
    obs = build_observation(state, N_MAX_BACKENDS, _NORM)
    assert obs.shape == (N_MAX_BACKENDS * 3,)
    # No padded health_flag=1.0 at expected positions (all healthy → 0.0)
    health_flags = obs[2::3]
    assert all(h == pytest.approx(0.0) for h in health_flags)


def test_obs_empty_state_all_padding():
    obs = build_observation([], N_MAX_BACKENDS, _NORM)
    assert obs.shape == (N_MAX_BACKENDS * 3,)
    assert np.allclose(obs[::3],  0.0)   # latency
    assert np.allclose(obs[1::3], 0.0)   # request_count
    assert np.allclose(obs[2::3], 1.0)   # health_flag (all padded = unhealthy)


# ── build_action_mask ─────────────────────────────────────────────────────────

def test_mask_all_healthy():
    state = [_b(f"b{i}", 50.0, 5, "healthy") for i in range(N_MAX_BACKENDS)]
    mask = build_action_mask(state, N_MAX_BACKENDS)
    assert mask.shape == (N_MAX_BACKENDS,)
    assert mask.dtype == bool
    assert mask.all()


def test_mask_mixed_health():
    state = [
        _b("a", 50.0, 5, "healthy"),
        _b("b", 50.0, 5, "degraded"),
        _b("c", 50.0, 5, "unhealthy"),
    ]
    mask = build_action_mask(state, N_MAX_BACKENDS)
    assert mask[0] is np.bool_(True)   # "a" healthy
    assert mask[1] is np.bool_(True)   # "b" degraded — still eligible
    assert mask[2] is np.bool_(False)  # "c" unhealthy — masked
    # Padding slots (3, 4) are False
    assert not mask[3]
    assert not mask[4]


def test_mask_all_unhealthy_returns_all_false():
    state = [_b(f"b{i}", 50.0, 5, "unhealthy") for i in range(3)]
    mask = build_action_mask(state, N_MAX_BACKENDS)
    assert not mask.any()


def test_mask_empty_state_all_false():
    mask = build_action_mask([], N_MAX_BACKENDS)
    assert not mask.any()
    assert mask.shape == (N_MAX_BACKENDS,)


def test_mask_degraded_is_eligible():
    """Degraded backends must remain in the eligible set — they're slower
    but still serving. Masking them out would concentrate load on healthy
    backends and worsen the imbalance."""
    state = [_b("b1", 300.0, 30, "degraded")]
    mask = build_action_mask(state, N_MAX_BACKENDS)
    assert mask[0] is np.bool_(True)


# ── all_masked_fallback ───────────────────────────────────────────────────────

def test_fallback_returns_exactly_one_true():
    state = [_b(f"b{i}", float(i * 100), 10, "unhealthy") for i in range(1, 4)]
    mask = all_masked_fallback(state, N_MAX_BACKENDS)
    assert mask.shape == (N_MAX_BACKENDS,)
    assert mask.sum() == 1


def test_fallback_picks_lowest_latency():
    state = [
        _b("a", 300.0, 10, "unhealthy"),
        _b("b", 100.0, 10, "unhealthy"),  # lowest latency
        _b("c", 200.0, 10, "unhealthy"),
    ]
    mask = all_masked_fallback(state, N_MAX_BACKENDS)
    # Sorted: a→slot0, b→slot1, c→slot2 — "b" has lowest latency
    assert mask[1] is np.bool_(True)
    assert not mask[0]
    assert not mask[2]


def test_fallback_empty_state_unmasks_slot_zero():
    mask = all_masked_fallback([], N_MAX_BACKENDS)
    assert mask[0] is np.bool_(True)
    assert mask.sum() == 1


def test_fallback_single_backend():
    state = [_b("only", 999.0, 0, "unhealthy")]
    mask = all_masked_fallback(state, N_MAX_BACKENDS)
    assert mask.sum() == 1
    assert mask[0] is np.bool_(True)


# ── NormParams ────────────────────────────────────────────────────────────────

def test_norm_params_round_trip():
    norm = NormParams(latency_scale=1000.0, request_count_scale=200.0)
    d = norm.to_dict()
    assert d == {"latency_scale": 1000.0, "request_count_scale": 200.0}
    restored = NormParams.from_dict(d)
    assert restored.latency_scale == 1000.0
    assert restored.request_count_scale == 200.0


def test_norm_params_from_dict_coerces_strings():
    d = {"latency_scale": "750.0", "request_count_scale": "150"}
    norm = NormParams.from_dict(d)
    assert isinstance(norm.latency_scale, float)
    assert isinstance(norm.request_count_scale, float)
    assert norm.latency_scale == pytest.approx(750.0)
    assert norm.request_count_scale == pytest.approx(150.0)
