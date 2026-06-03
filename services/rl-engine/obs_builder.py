"""
services/rl-engine/obs_builder.py
──────────────────────────────────
Shared observation / action-mask builder.

Consumed at two sites:
  training/env.py        — SmartLoadEnv.observation_space, reset(), step()
  policies/ppo/policy.py — PPOPolicy.act() during serving

Observation tensor layout: (N_MAX_BACKENDS × 3,) float32.
Per-backend slot: [latency_ms_norm, request_count_norm, health_flag]
  latency_ms_norm    = latency_ms  / NormParams.latency_scale
  request_count_norm = queue_depth / NormParams.request_count_scale
  health_flag        = 0.0 (healthy) | 0.5 (degraded) | 1.0 (unhealthy)

Absent backends (live count < n_max) are padded as [0.0, 0.0, 1.0]:
zero load with max health penalty — PPO never prefers a padded slot.

NOTE: BackendState.queue_depth is SUM(request_count) from RL_STATE_QUERY,
not a true queue depth. The field name is preserved for compatibility with
existing tests; normalization treats it as a load proxy.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from policy_base import BackendState

N_MAX_BACKENDS: int = 5  # must equal policy.yaml max_backends

_HEALTH_FLAG: dict[str, float] = {
    "healthy":   0.0,
    "degraded":  0.5,
    "unhealthy": 1.0,
}

_log = logging.getLogger(__name__)


@dataclass
class NormParams:
    """Normalization scales written into artifact_meta.json at training time
    and read back by PPOPolicy at serving time."""

    latency_scale: float
    request_count_scale: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "NormParams":
        return cls(
            latency_scale=float(d["latency_scale"]),
            request_count_scale=float(d["request_count_scale"]),
        )


def build_observation(
    state: list[BackendState],
    n_max: int,
    norm: NormParams,
) -> np.ndarray:
    """Return a flat float32 array of shape (n_max * 3,).

    Backends are sorted by backend_id for stable slot assignment across calls.
    Slots beyond len(state) are padded as [0.0, 0.0, 1.0].
    """
    obs = np.zeros(n_max * 3, dtype=np.float32)
    sorted_state = sorted(state, key=lambda s: s.backend_id)

    for i, s in enumerate(sorted_state[:n_max]):
        base = i * 3
        obs[base]     = s.latency_ms  / norm.latency_scale
        obs[base + 1] = s.queue_depth / norm.request_count_scale
        obs[base + 2] = _HEALTH_FLAG.get(s.health, 1.0)

    # Padding slots: latency and request_count stay 0.0 (already from zeros),
    # health_flag must be 1.0 to signal "absent = unhealthy".
    for i in range(len(sorted_state), n_max):
        obs[i * 3 + 2] = 1.0

    return obs


def build_action_mask(
    state: list[BackendState],
    n_max: int,
) -> np.ndarray:
    """Return a bool array of shape (n_max,).

    True  = backend is eligible (healthy or degraded).
    False = backend is masked out (unhealthy, unknown, or absent / padded slot).

    The eligibility predicate lives in policy_base.is_eligible — kept central
    so all serving paths (this mask, ranking filters in PPO/round_robin/
    least_connections, sidecar reweighting) agree on what "eligible" means.

    Caller must check for all-False and invoke all_masked_fallback() before
    passing the mask to MaskablePPO — SB3 will raise on an all-False mask.
    """
    # Late import to avoid a hard dependency cycle between obs_builder and
    # policy_base in test setups that import only obs_builder.
    from policy_base import is_eligible

    mask = np.zeros(n_max, dtype=bool)
    sorted_state = sorted(state, key=lambda s: s.backend_id)
    for i, s in enumerate(sorted_state[:n_max]):
        mask[i] = is_eligible(s.health)
    return mask


def all_masked_fallback(
    state: list[BackendState],
    n_max: int,
) -> np.ndarray:
    """Return a mask with exactly one True entry.

    Called only when build_action_mask() returns all False (every live backend
    is unhealthy). Unmasks the backend with the lowest latency — the least-bad
    choice. If state is empty, unmasks slot 0 as a last resort.
    """
    _log.warning(
        "all_masked_fallback: all %d live backends are unhealthy; "
        "unmasking lowest-latency backend",
        len(state),
    )
    mask = np.zeros(n_max, dtype=bool)
    if not state:
        mask[0] = True
        return mask

    sorted_state = sorted(state, key=lambda s: s.backend_id)
    clipped = sorted_state[:n_max]
    best = min(range(len(clipped)), key=lambda i: clipped[i].latency_ms)
    mask[best] = True
    return mask
