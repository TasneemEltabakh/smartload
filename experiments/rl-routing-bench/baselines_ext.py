"""
experiments/rl-routing-bench/baselines_ext.py
──────────────────────────────────────────────
Four additional STRONG classical load-balancing baselines for the routing
benchmark, beyond round_robin / least_connections / random_shadow. Each is
rendered as the weight vector the LB sidecar would apply for that window, over
eligible (healthy/degraded) backends in canonical sorted-backend_id slot order.

  join_shortest_queue (JSQ)
      Faithful per-request rendering: starting from each backend's observed load
      (queue_depth), assign R window "request tokens" one at a time, each to the
      backend with the currently-smallest projected queue; weight = tokens/R.
      This is exactly what per-request JSQ converges to over a window.

  power_of_two_choices (P2C)
      Same per-request loop, but each token samples TWO eligible backends at
      random and joins the shorter — the classic "the power of two random
      choices" rule. Seeded per episode for determinism.

  least_response_time (LRT)
      Routes inversely to observed response time: weight_i ∝ (1/latency_i)**p.
      The smooth weight rendering of "prefer the fastest-responding backend".

  weighted_least_connections (WLC)
      Routes inversely to current connections/load: weight_i ∝ 1/(load_i + 1).

All four exclude unhealthy/unknown/absent backends (is_eligible) and fall back to
uniform-over-eligible when no signal is available. They are memoryless per window
(recomputed from the observed state each window); P2C carries only a seeded RNG so
the benchmark is deterministic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[2]
_RL_ENGINE = _REPO / "services" / "rl-engine"
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))

from obs_builder import N_MAX_BACKENDS                     # noqa: E402
from policy_base import BackendState, is_eligible          # noqa: E402

_MIN_TOKENS = 64   # floor on within-window request tokens for JSQ / P2C rendering


def _n_tokens(load, mask):
    """Number of within-window arrival tokens to assign for JSQ/P2C. The window's
    incoming arrivals ≈ recent total load (the only demand signal available), so a
    proper queue-equalisation needs roughly that many tokens; floored for low load."""
    return int(max(_MIN_TOKENS, round(float(load[mask].sum()))))


def _slots(sim_state):
    """lat, load, mask in canonical (sorted backend_id) slot order, length n."""
    ss = sorted(sim_state, key=lambda s: s.backend_id)
    n = min(len(ss), N_MAX_BACKENDS)
    lat = np.full(N_MAX_BACKENDS, np.inf)
    load = np.zeros(N_MAX_BACKENDS)
    mask = np.zeros(N_MAX_BACKENDS, dtype=bool)
    for i in range(n):
        lat[i] = ss[i].latency_ms
        load[i] = ss[i].queue_depth
        mask[i] = is_eligible(ss[i].health)
    return lat, load, mask


def _uniform(mask):
    w = mask.astype(float)
    return w / w.sum() if w.sum() else np.ones(N_MAX_BACKENDS) / N_MAX_BACKENDS


class _Base:
    """Stateful contender constructed fresh per (scenario, seed-band, episode)."""
    def __init__(self, seed=None):
        self._rng = np.random.default_rng(seed)
    # weight_fn(obs, sim_state) signature to match the harness adapters.
    def __call__(self, obs, sim_state):
        return self.weights(sim_state)
    def weights(self, sim_state):
        raise NotImplementedError


class JoinShortestQueue(_Base):
    def weights(self, sim_state):
        lat, load, mask = _slots(sim_state)
        elig = np.where(mask)[0]
        if elig.size == 0:
            return _uniform(mask)
        q = load.copy().astype(float)
        assigned = np.zeros(N_MAX_BACKENDS)
        for _ in range(_n_tokens(load, mask)):
            j = elig[np.argmin(q[elig])]
            assigned[j] += 1.0
            q[j] += 1.0
        w = np.where(mask, assigned, 0.0)
        return w / w.sum() if w.sum() else _uniform(mask)


class PowerOfTwoChoices(_Base):
    def weights(self, sim_state):
        lat, load, mask = _slots(sim_state)
        elig = np.where(mask)[0]
        if elig.size == 0:
            return _uniform(mask)
        q = load.copy().astype(float)
        assigned = np.zeros(N_MAX_BACKENDS)
        for _ in range(_n_tokens(load, mask)):
            if elig.size == 1:
                j = elig[0]
            else:
                a, b = self._rng.choice(elig, size=2, replace=False)
                j = a if q[a] <= q[b] else b
            assigned[j] += 1.0
            q[j] += 1.0
        w = np.where(mask, assigned, 0.0)
        return w / w.sum() if w.sum() else _uniform(mask)


class LeastResponseTime(_Base):
    def __init__(self, seed=None, p=2.0):
        super().__init__(seed)
        self.p = p
    def weights(self, sim_state):
        lat, load, mask = _slots(sim_state)
        latc = np.clip(np.where(mask, lat, np.inf), 1e-3, None)
        w = np.where(mask, (1.0 / latc) ** self.p, 0.0)
        return w / w.sum() if w.sum() else _uniform(mask)


class WeightedLeastConnections(_Base):
    def weights(self, sim_state):
        lat, load, mask = _slots(sim_state)
        w = np.where(mask, 1.0 / (load + 1.0), 0.0)
        return w / w.sum() if w.sum() else _uniform(mask)


# name -> constructor(seed) factory, matching classical_factory() shape.
def factory():
    return {
        "join_shortest_queue": lambda seed=None: JoinShortestQueue(seed),
        "power_of_two_choices": lambda seed=None: PowerOfTwoChoices(seed),
        "least_response_time": lambda seed=None: LeastResponseTime(seed),
        "weighted_least_connections": lambda seed=None: WeightedLeastConnections(seed),
    }
