"""
services/rl-engine/training/env_discrete_templates.py
──────────────────────────────────────────────────────
Discrete-action wrapper env for value-based RL (DQN) on the closed-loop routing
MDP.

WHY THIS EXISTS
───────────────
DQN is a value-based method over a *discrete* action set; it cannot emit the
continuous weight vector that SmartLoadEnvV2 (env_v2) expects. To give DQN a fair
shot at the same problem, this env exposes Discrete(K), where each action picks
one of K fixed routing TEMPLATES that are computed from the *current* observed
state each window:

  [0] uniform              — round-robin / even split over eligible backends
  [1] inverse-latency      — weight ∝ 1 / observed latency over eligible
  [2] exclude-slowest      — drop the single slowest eligible backend, uniform on rest
  [3] concentrate-fastest  — all weight on the single fastest eligible backend

Everything else is shared with env_v2 so the comparison is apples-to-apples:
the SAME ClosedLoopSimulator, the SAME reward_v2.compute_reward, the SAME
build_observation / DEFAULT_NORM. The only difference is the action interface:
DQN chooses *which template* to apply, classical-router style, rather than
free-form logits.

Observation is byte-for-byte build_observation, so train/serve parity holds.
This env is training-only; never COPY'd into the runtime image.
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
from training.env_v2 import DEFAULT_NORM                                          # noqa: E402

# Routing templates live in the serving-safe `routing_templates` module so that
# training (this env) and serving (policies/ppo/policy.py DQN branch) share ONE
# definition and cannot drift apart. (The logic is unchanged from when the
# algorithm comparison was run — it was extracted verbatim.)
from routing_templates import N_TEMPLATES, template_weights  # noqa: E402,F401


class SmartLoadDiscreteTemplatesEnv(gym.Env):
    """Discrete(K) routing env: each action selects a routing template applied to
    the current window. Shares ClosedLoopSimulator + reward_v2 with env_v2."""

    metadata = {"render_modes": []}

    def __init__(
        self,
        n_backends: int = N_MAX_BACKENDS,
        norm: NormParams = DEFAULT_NORM,
        episode_length: int = DEFAULT_EPISODE_LENGTH,
        reward_cfg: RewardConfig | None = None,
        n_templates: int = N_TEMPLATES,
    ) -> None:
        super().__init__()
        self._sim = ClosedLoopSimulator(n_backends, episode_length=episode_length)
        self._n = n_backends
        self._norm = norm
        self._reward_cfg = reward_cfg or RewardConfig()
        self._n_templates = n_templates
        self._state: list = []

        self.observation_space = spaces.Box(
            low=0.0, high=np.inf, shape=(N_MAX_BACKENDS * 3,), dtype=np.float32
        )
        self.action_space = spaces.Discrete(n_templates)

    def reset(self, *, seed: int | None = None, options: dict | None = None):
        super().reset(seed=seed)
        self._state = self._sim.reset(seed=seed)
        return self._build_obs(self._state), {}

    def step(self, action):
        template = int(np.asarray(action).reshape(-1)[0])
        w = template_weights(template, self._state, self._n)
        next_state, metrics, done = self._sim.step(w)
        reward = compute_reward(metrics, self._reward_cfg)
        self._state = next_state
        info = {"scenario": self._sim.scenario_kind,
                "served_lat_ms": metrics.served_mean_latency_ms,
                "shed": metrics.shed_fraction,
                "template": template}
        return self._build_obs(next_state), reward, done, False, info

    def _build_obs(self, state):
        return build_observation(state, N_MAX_BACKENDS, self._norm)
