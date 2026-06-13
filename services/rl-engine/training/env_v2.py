"""
services/rl-engine/training/env_v2.py
──────────────────────────────────────
SmartLoadEnvV2 — closed-loop MDP for routing-policy training.

Differences from env.py (the open-loop bandit env):
  • Action is a WEIGHT VECTOR, not a single backend. action_space is
    Box(N_MAX_BACKENDS); the raw action is turned into a simplex via masked
    softmax (unhealthy backends get zero weight), so the policy can express
    "spread evenly" or "shift away from the slow one".
  • Dynamics are causal: ClosedLoopSimulator splits the window's demand by those
    weights and computes each backend's latency from its queue. The reward
    (reward_v2) is the consequence of the agent's own routing.

Observation is byte-for-byte the same as serving (obs_builder.build_observation),
so train/serve parity holds. This env is training-only; never COPY'd into the
runtime image.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import gymnasium as gym
from gymnasium import spaces

_RL_ENGINE = Path(__file__).resolve().parents[1]
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))

from obs_builder import (   # noqa: E402
    N_MAX_BACKENDS, NormParams,
    build_observation, build_action_mask, all_masked_fallback,
)
from training.closed_loop_sim import ClosedLoopSimulator, DEFAULT_EPISODE_LENGTH  # noqa: E402
from training.reward_v2 import RewardConfig, compute_reward                       # noqa: E402

# Latency_scale 200ms (~SLO); request_count_scale ~ per-window per-backend load.
DEFAULT_NORM = NormParams(latency_scale=200.0, request_count_scale=100.0)
_ACTION_BOUND = 10.0


def action_to_weights(action: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Masked softmax: raw action vector → simplex weights over eligible
    backends. Masked (unhealthy) slots get zero weight."""
    logits = np.asarray(action, dtype=float)
    masked = np.where(mask, logits, -1e9)
    masked = masked - masked.max()
    e = np.exp(masked)
    total = e.sum()
    if not np.isfinite(total) or total <= 0:
        # degenerate — fall back to uniform over eligible
        w = mask.astype(float)
        return w / w.sum() if w.sum() > 0 else np.ones_like(w) / len(w)
    return e / total


class SmartLoadEnvV2(gym.Env):
    metadata = {"render_modes": []}

    def __init__(
        self,
        n_backends: int = N_MAX_BACKENDS,
        norm: NormParams = DEFAULT_NORM,
        episode_length: int = DEFAULT_EPISODE_LENGTH,
        reward_cfg: RewardConfig | None = None,
    ) -> None:
        super().__init__()
        self._sim = ClosedLoopSimulator(n_backends, episode_length=episode_length)
        self._n = n_backends
        self._norm = norm
        self._reward_cfg = reward_cfg or RewardConfig()
        self._state: list = []

        self.observation_space = spaces.Box(
            low=0.0, high=np.inf, shape=(N_MAX_BACKENDS * 3,), dtype=np.float32
        )
        self.action_space = spaces.Box(
            low=-_ACTION_BOUND, high=_ACTION_BOUND,
            shape=(N_MAX_BACKENDS,), dtype=np.float32,
        )

    def reset(self, *, seed: int | None = None, options: dict | None = None):
        super().reset(seed=seed)
        self._state = self._sim.reset(seed=seed)
        return self._build_obs(self._state), {}

    def step(self, action):
        mask = build_action_mask(self._state, N_MAX_BACKENDS)
        if not mask.any():
            mask = all_masked_fallback(self._state, N_MAX_BACKENDS)
        # The sim tracks `self._n` live backends; restrict to those slots.
        w = action_to_weights(np.asarray(action)[: self._n], mask[: self._n])
        next_state, metrics, done = self._sim.step(w)
        reward = compute_reward(metrics, self._reward_cfg)
        self._state = next_state
        info = {"scenario": self._sim.scenario_kind,
                "served_lat_ms": metrics.served_mean_latency_ms,
                "shed": metrics.shed_fraction}
        return self._build_obs(next_state), reward, done, False, info

    def _build_obs(self, state):
        return build_observation(state, N_MAX_BACKENDS, self._norm)
