"""
services/rl-engine/routing_templates.py
─────────────────────────────────────────
Serving-safe routing templates shared by the DQN-templates policy (serving) and
the discrete-templates training env. Imports ONLY obs_builder (no training/*),
so it can be COPY'd into the runtime image.

A "template" maps the current per-backend state to a weight vector using a fixed,
interpretable rule. The DQN policy chooses *which* template to apply each cycle.

  [0] uniform              — even split over eligible backends (round-robin)
  [1] inverse-latency      — weight ∝ 1 / observed latency over eligible
  [2] exclude-slowest      — drop the single slowest eligible, uniform on rest
  [3] concentrate-fastest  — all weight on the single fastest eligible
"""

from __future__ import annotations

import numpy as np

from obs_builder import N_MAX_BACKENDS, build_action_mask, all_masked_fallback

N_TEMPLATES: int = 4


def template_weights(template: int, state: list, n: int) -> np.ndarray:
    """Map a template id + current `state` (list[BackendState]) to a normalised
    weight vector over the n live backends (sorted by backend_id, the canonical
    order). Only eligible (healthy/degraded) backends receive weight."""
    mask = build_action_mask(state, N_MAX_BACKENDS)
    if not mask.any():
        mask = all_masked_fallback(state, N_MAX_BACKENDS)
    mask = mask[:n]

    sorted_state = sorted(state, key=lambda s: s.backend_id)[:n]
    lat = np.array([s.latency_ms for s in sorted_state], dtype=float)
    if lat.shape[0] < n:
        lat = np.concatenate([lat, np.full(n - lat.shape[0], np.inf)])

    elig = np.where(mask)[0]
    w = np.zeros(n, dtype=float)
    if len(elig) == 0:
        return np.ones(n, dtype=float) / n

    if template == 1:
        w[elig] = 1.0 / np.maximum(lat[elig], 1.0)
    elif template == 2:
        # Exclude the slowest eligible ONLY if it is a genuine outlier
        # (meaningfully slower than the median). On a homogeneous / idle pool no
        # backend qualifies, so this degrades to uniform — which keeps "exclude"
        # from needlessly dropping a healthy backend (the Gate-A / idle issue).
        if len(elig) <= 1:
            w[elig] = 1.0
        else:
            med = float(np.median(lat[elig]))
            slowest = elig[int(np.argmax(lat[elig]))]
            if lat[slowest] > 1.5 * max(med, 1.0):
                w[[i for i in elig if i != slowest]] = 1.0
            else:
                w[elig] = 1.0
    elif template == 3:
        w[elig[int(np.argmin(lat[elig]))]] = 1.0
    else:  # 0 / default → uniform
        w[elig] = 1.0

    total = w.sum()
    return w / total if total > 0 else np.ones(n, dtype=float) / n
