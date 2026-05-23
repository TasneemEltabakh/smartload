"""
tests/unit/rl-engine/test_env.py
─────────────────────────────────
Unit tests for services/rl-engine/training/env.py.

No Docker, no Redis, no DB.  Uses a tiny in-memory dataset for speed.

N2.3 Acceptance Criteria exercised here:
  AC1: gymnasium.utils.env_checker.check_env(SmartLoadEnv(...)) passes
  AC2: env.observation_space.shape == (15,)  (N_MAX_BACKENDS=5, 3 features)
  AC3: env.action_space.n == 5
  AC4: 5-step episode completes without exception using synthetic state
  AC5: action_masks() returns all-False for all-unhealthy; all_masked_fallback
       invoked → exactly one True
"""

from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "rl-engine"
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from obs_builder import N_MAX_BACKENDS, NormParams   # noqa: E402
from policy_base import BackendState                  # noqa: E402
from training.dataset import TraceReplayDataset       # noqa: E402
from training.env import SmartLoadEnv                 # noqa: E402


# ── synthetic dataset fixture ─────────────────────────────────────────────────

def _make_csv(tmp_path: Path, n_rows: int = 300, n_backends: int = 3) -> Path:
    """Write a tiny Alibaba-format CSV to tmp_path."""
    p = tmp_path / "trace.csv"
    fieldnames = ["traceid", "timestamp", "rpcid", "um", "rpctype", "dm", "interface", "rt"]
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for i in range(n_rows):
            # Spread across 3 backends over 120 seconds
            dm  = f"backend_hash_{i % n_backends:02d}"
            ts  = (i // n_backends) * 1000   # one row per backend per second
            rt  = 50.0 + (i % 20)            # varying latency 50–69 ms
            w.writerow({"traceid": "t", "timestamp": ts, "rpcid": "0.1",
                        "um": "caller", "rpctype": "http", "dm": dm,
                        "interface": "", "rt": rt})
    return p


@pytest.fixture
def tiny_env(tmp_path):
    csv_path = _make_csv(tmp_path)
    ds   = TraceReplayDataset([csv_path], n_backends=N_MAX_BACKENDS, window_ms=5_000)
    norm = NormParams(latency_scale=500.0, request_count_scale=100.0)
    return SmartLoadEnv(dataset=ds, norm=norm, episode_length=5)


# ── AC1: check_env ────────────────────────────────────────────────────────────

def test_check_env_passes(tiny_env):
    """gymnasium.utils.env_checker.check_env must pass with no warnings."""
    from gymnasium.utils.env_checker import check_env
    check_env(tiny_env, warn=True, skip_render_check=True)


# ── AC2 + AC3: spaces ────────────────────────────────────────────────────────

def test_observation_space_shape(tiny_env):
    assert tiny_env.observation_space.shape == (N_MAX_BACKENDS * 3,)


def test_observation_space_dtype(tiny_env):
    assert tiny_env.observation_space.dtype == np.float32


def test_action_space_n(tiny_env):
    assert tiny_env.action_space.n == N_MAX_BACKENDS


# ── AC4: episode completion ───────────────────────────────────────────────────

def test_reset_returns_valid_obs(tiny_env):
    obs, info = tiny_env.reset(seed=0)
    assert obs.shape == (N_MAX_BACKENDS * 3,)
    assert obs.dtype == np.float32
    assert isinstance(info, dict)


def test_five_step_episode_no_exception(tiny_env):
    obs, _ = tiny_env.reset(seed=42)
    for step in range(5):
        action = tiny_env.action_space.sample()
        obs, reward, terminated, truncated, info = tiny_env.step(action)
        assert obs.shape == (N_MAX_BACKENDS * 3,)
        assert isinstance(reward, float)
        assert isinstance(terminated, bool)
        assert isinstance(truncated, bool)
        if terminated or truncated:
            break


def test_episode_terminates_after_episode_length(tiny_env):
    tiny_env.reset(seed=0)
    done = False
    steps = 0
    while not done:
        _, _, terminated, truncated, _ = tiny_env.step(0)
        done = terminated or truncated
        steps += 1
        assert steps <= tiny_env._sim.episode_length + 5, "episode did not terminate"
    assert done


# ── AC5: action masking ───────────────────────────────────────────────────────

def test_action_masks_shape(tiny_env):
    tiny_env.reset(seed=0)
    mask = tiny_env.action_masks()
    assert mask.shape == (N_MAX_BACKENDS,)
    assert mask.dtype == bool


def test_action_masks_all_unhealthy_uses_fallback(tmp_path):
    """When all backends are unhealthy, action_masks() must return exactly
    one True via all_masked_fallback()."""
    # Inject an all-unhealthy state directly
    env = SmartLoadEnv.__new__(SmartLoadEnv)
    env._norm   = NormParams(500.0, 100.0)
    env._state  = [
        BackendState(f"b{i}", 999.0, 5, "unhealthy") for i in range(N_MAX_BACKENDS)
    ]

    # Provide a dummy _sim to satisfy attribute access
    class _DummySim:
        episode_length = 5
    env._sim = _DummySim()

    from training.reward import RewardCalculator
    env._reward = RewardCalculator(norm=env._norm)

    mask = env.action_masks()
    assert mask.shape == (N_MAX_BACKENDS,)
    assert mask.sum() == 1, "all-unhealthy state must produce exactly one True via fallback"


def test_action_masks_healthy_backends_are_true(tiny_env):
    """At least one backend must be eligible after reset (not all padded)."""
    tiny_env.reset(seed=0)
    mask = tiny_env.action_masks()
    # The synthetic dataset has healthy backends, so at least one must be True
    assert mask.any(), "Expected at least one eligible backend after reset"


# ── obs values ────────────────────────────────────────────────────────────────

def test_obs_in_observation_space(tiny_env):
    obs, _ = tiny_env.reset(seed=0)
    assert tiny_env.observation_space.contains(obs), (
        f"Observation {obs} not in observation_space {tiny_env.observation_space}"
    )
