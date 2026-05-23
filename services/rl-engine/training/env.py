"""
services/rl-engine/training/env.py
────────────────────────────────────
SmartLoadEnv — Gymnasium environment for offline PPO training.

Canonical path per SOT §8.7 (updated from root-level to training/env.py).
This file is training-only and is never COPY'd into the runtime Docker image.

Observation space: Box((N_MAX_BACKENDS * 3,), float32)
  Flat vector of per-backend features: [latency_norm, req_count_norm, health_flag]
  Built by obs_builder.build_observation() — the same function used at serving
  time by PPOPolicy, guaranteeing train/serve parity.

Action space: Discrete(N_MAX_BACKENDS)
  Index of the backend to route to (0-based, sorted by backend_id).

Action masking:
  Implemented via action_masks() for use with sb3-contrib MaskablePPO.
  All backends marked "unhealthy" are masked out.  When all backends are
  unhealthy, all_masked_fallback() unmasks the least-bad one.

Episode structure:
  reset() samples a random start timestamp from the dataset (reproducible
  with seed).  Each step() advances one 30-second window.  Episodes terminate
  after episode_length steps or when the dataset runs out of windows.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import gymnasium as gym
from gymnasium import spaces

_RL_ENGINE = Path(__file__).resolve().parents[1]
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))

from obs_builder import (          # noqa: E402
    N_MAX_BACKENDS,
    NormParams,
    all_masked_fallback,
    build_action_mask,
    build_observation,
)
from policy_base import BackendState                     # noqa: E402
from training.dataset import TraceReplayDataset          # noqa: E402
from training.reward import RewardCalculator             # noqa: E402
from training.simulator import BackendSimulator, DEFAULT_EPISODE_LENGTH  # noqa: E402

# Default NormParams — calibrated on Alibaba p99 latency (~2000 ms) and
# typical per-window request count (~200 requests in 30 s at moderate load).
# These are overridden at training time once the dataset statistics are known;
# the defaults ensure the environment is usable out-of-the-box for smoke tests.
DEFAULT_NORM = NormParams(latency_scale=2000.0, request_count_scale=200.0)


class SmartLoadEnv(gym.Env):
    """Gymnasium environment for SmartLoad RL routing.

    Parameters
    ----------
    dataset:
        Loaded TraceReplayDataset.  If None, a tiny synthetic dataset is
        constructed from the first available Alibaba partition so the
        environment passes check_env without requiring manual setup.
    norm:
        NormParams for observation normalization.
    episode_length:
        Maximum steps per episode.
    imbalance_lambda:
        Weight of the load-imbalance penalty in the reward function.
    """

    metadata = {"render_modes": []}

    def __init__(
        self,
        dataset: TraceReplayDataset | None = None,
        norm: NormParams = DEFAULT_NORM,
        episode_length: int = DEFAULT_EPISODE_LENGTH,
        imbalance_lambda: float = 0.1,
    ) -> None:
        super().__init__()

        if dataset is None:
            dataset = _default_dataset()

        self._sim    = BackendSimulator(dataset, episode_length=episode_length)
        self._norm   = norm
        self._reward = RewardCalculator(norm=norm, imbalance_lambda=imbalance_lambda)
        self._state: list[BackendState] = []

        obs_dim = N_MAX_BACKENDS * 3
        self.observation_space = spaces.Box(
            low=0.0, high=np.inf, shape=(obs_dim,), dtype=np.float32
        )
        self.action_space = spaces.Discrete(N_MAX_BACKENDS)

    # ── Gymnasium API ─────────────────────────────────────────────────────────

    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict | None = None,
    ) -> tuple[np.ndarray, dict]:
        super().reset(seed=seed)
        self._state = self._sim.reset(seed=seed)
        obs = self._build_obs(self._state)
        return obs, {}

    def step(self, action: int) -> tuple[np.ndarray, float, bool, bool, dict]:
        pre_state = self._state
        next_state, done = self._sim.step(action)

        reward    = self._reward.compute(pre_state, action, next_state)
        self._state = next_state

        obs        = self._build_obs(next_state)
        terminated = done
        truncated  = False
        info: dict[str, Any] = {}

        return obs, reward, terminated, truncated, info

    def action_masks(self) -> np.ndarray:
        """Return action mask for MaskablePPO (True = eligible backend).

        Called by sb3-contrib before each action selection.
        """
        mask = build_action_mask(self._state, N_MAX_BACKENDS)
        if not mask.any():
            mask = all_masked_fallback(self._state, N_MAX_BACKENDS)
        return mask

    def render(self) -> None:
        pass

    # ── internal ──────────────────────────────────────────────────────────────

    def _build_obs(self, state: list[BackendState]) -> np.ndarray:
        return build_observation(state, N_MAX_BACKENDS, self._norm)


# ── convenience factory ───────────────────────────────────────────────────────

def _default_dataset() -> TraceReplayDataset:
    """Return a dataset from the first available Alibaba partition.

    Used when SmartLoadEnv() is constructed without an explicit dataset — e.g.
    during check_env() calls, unit tests, or quick smoke runs.
    """
    alibaba_dir = Path(__file__).resolve().parents[3] / "datasets" / "alibaba"
    partitions  = sorted(alibaba_dir.glob("*.csv"))
    if not partitions:
        raise FileNotFoundError(
            f"No Alibaba CSV partitions found at {alibaba_dir}. "
            "Clone the dataset or pass a dataset= argument explicitly."
        )
    return TraceReplayDataset([partitions[0]], n_backends=N_MAX_BACKENDS)
