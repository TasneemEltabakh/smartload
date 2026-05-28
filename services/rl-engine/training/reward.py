"""
services/rl-engine/training/reward.py
───────────────────────────────────────
RewardCalculator — computes the per-step reward for SmartLoadEnv.

Formula
───────
    reward = -(latency_ms_chosen / latency_scale) - λ × load_imbalance
    load_imbalance = std(request_counts) / (mean(request_counts) + ε)

where:
  latency_ms_chosen  — latency of the CHOSEN backend in next_state.
  latency_scale      — normalization constant (from NormParams).
  λ                  — imbalance penalty weight (default 0.1).
  ε                  — numerical stability term (default 1.0).

Bandit framing (read simulator.py docstring first)
──────────────────────────────────────────────────
The simulator replays trace windows independently of the agent's action,
so latency_ms_chosen is the recorded next-window latency of whichever
backend the policy picked — NOT a causal consequence of having routed to
it. The reward signal trains the policy to be a good predictor of which
backend will have low latency next, given the current state. It is not a
learned model of how routing decisions perturb the system.

This makes the imbalance term a regulariser on the *trace's* historical
spread, not a learned consequence of the policy's weighting. We keep it
because (a) it discourages picks that bias the policy toward backends
whose contemporaneous load is already an outlier, and (b) it produces a
smoother reward signal during training. Removing it didn't improve held-out
metrics during the N2.4 canary.

Hard penalty:
  Choosing an UNHEALTHY backend incurs an additional -10.0 penalty.  This
  should never happen in practice (the action mask prevents it), but provides
  defence-in-depth — if the mask is ever misconfigured the agent receives a
  strong negative signal.

Called only from SmartLoadEnv.step() — never from serving code.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_RL_ENGINE = Path(__file__).resolve().parents[1]
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))

from obs_builder import NormParams     # noqa: E402
from policy_base import BackendState   # noqa: E402

_UNHEALTHY_PENALTY: float = -10.0
_EPSILON: float = 1.0


class RewardCalculator:
    """Stateless reward function.

    Parameters
    ----------
    norm:
        NormParams used to scale latency — same object used by obs_builder so
        the reward signal and the observation are on the same scale.
    imbalance_lambda:
        Weight of the load-imbalance penalty term (default 0.1).
    """

    def __init__(
        self,
        norm: NormParams,
        imbalance_lambda: float = 0.1,
    ) -> None:
        self._norm              = norm
        self._imbalance_lambda  = imbalance_lambda

    def compute(
        self,
        state: list[BackendState],      # pre-decision state (kept for API symmetry)
        action: int,
        next_state: list[BackendState], # next replay window — latency read from HERE
    ) -> float:
        """Return the scalar reward for taking `action` in `state`.

        Parameters
        ----------
        state:
            Pre-decision system state. Not used for the latency term, but
            available for future extensions (e.g. delta-from-prior bonuses).
        action:
            Index into the sorted backend list (0-based).  Corresponds to the
            action space index in SmartLoadEnv.
        next_state:
            Next replayed window from the dataset. The chosen backend's
            latency is read from here as an observational signal — this is
            the bandit framing documented in simulator.py, not a closed-loop
            consequence of the routing decision.
        """
        if not next_state:
            return 0.0

        sorted_next = sorted(next_state, key=lambda s: s.backend_id)

        if action < 0 or action >= len(sorted_next):
            return _UNHEALTHY_PENALTY

        chosen = sorted_next[action]

        # Hard penalty for routing to an unhealthy backend
        health_penalty = _UNHEALTHY_PENALTY if chosen.health == "unhealthy" else 0.0

        # Latency term (negative — lower latency is better)
        latency_term = -(chosen.latency_ms / self._norm.latency_scale)

        # Load-imbalance term
        counts = np.array([s.queue_depth for s in sorted_next], dtype=float)
        mean_count = counts.mean()
        imbalance = counts.std() / (mean_count + _EPSILON)
        imbalance_term = -self._imbalance_lambda * imbalance

        return float(latency_term + imbalance_term + health_penalty)
