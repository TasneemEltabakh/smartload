"""
services/rl-engine/training/monotone_router.py
───────────────────────────────────────────────
Core of the latency-MONOTONE routing controller (the production-recommended
policy). Pure-numpy; imported by both the serving plugin
(policies/monotone/policy.py) and the trainer (train_monotone.py) and the
benchmark adapter, so train / serve / eval share ONE implementation.

Why this design
───────────────
candidate_v2 (a free-form PPO) is adaptive but NOT monotone: on some states it
routes MORE traffic to a HIGHER-latency backend. That is the wrong thing for a
load balancer and is brittle when a backend degrades into a region the net never
saw. This controller is monotone-in-latency BY CONSTRUCTION and is robust by
design, while still being adaptive (capacity-aware + degradation-aware).

The routing target each window is

    cap_i   = 1 / base_i                         base_i = running-min latency
                                                 (≈ the backend's idle service time,
                                                  i.e. an online capacity estimate)
    degr_i  = lat_i / base_i                      how slow the backend is *right now*
                                                 relative to its own best (>=1)
    score_i = cap_i / degr_i**degr_pow           capacity-proportional, divided down
                                                 by current slowness
    score_i *= 1e-3  if  lat_i > cut * min_lat    hard-shed clearly-bad backends

    weights = score / sum(score)   over eligible (unhealthy/absent get 0)

then damped across windows  w_t = (1-a) w_{t-1} + a * target  to remove the
closed-loop oscillation a memoryless inverse-latency rule suffers (a -> 1 when
the pool is near-idle, so idle latency stays minimal).

Monotonicity: holding history fixed, score_i is strictly decreasing in the
current latency lat_i (through degr_i and the cut), and base_i / cap_i depend on
PAST latencies only — so weight is non-increasing in current latency. On a fresh
instance (running-min == current latency) the controller reduces to pure
inverse-latency, which is trivially monotone — this is the state the probe tests.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

_RL_ENGINE = Path(__file__).resolve().parents[1]
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))

from obs_builder import N_MAX_BACKENDS  # noqa: E402


@dataclass
class MonotoneConfig:
    degr_pow: float = 1.0     # exponent on current-slowness (degr_i)
    alpha: float = 0.4        # damping factor under load (0..1)
    cut: float = 3.0          # hard-shed backends with lat > cut * min_lat
    cap_floor_ms: float = 5.0 # floor on the capacity-estimate latency
    idle_load: float = 8.0    # total observed load below which damping is bypassed

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MonotoneConfig":
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


class MonotoneRouter:
    """Stateful latency-monotone router. One instance per routing session /
    benchmark episode (carries the running-min capacity estimate and the damped
    weight vector). Pure function of the observed backend state."""

    def __init__(self, cfg: MonotoneConfig | None = None, n: int = N_MAX_BACKENDS):
        self.cfg = cfg or MonotoneConfig()
        self.n = n
        self._minlat = np.full(n, np.inf)
        self._w: np.ndarray | None = None

    def reset(self) -> None:
        self._minlat = np.full(self.n, np.inf)
        self._w = None

    @staticmethod
    def _normalize(w: np.ndarray, mask: np.ndarray, n: int) -> np.ndarray:
        w = np.where(mask, np.clip(w, 0.0, None), 0.0)
        t = w.sum()
        if t <= 0:
            w = mask.astype(float)
            return w / w.sum() if w.sum() else np.ones(n) / n
        return w / t

    def weights(self, lat: np.ndarray, load: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """lat, load, mask are length-n arrays in canonical (sorted backend_id)
        slot order. Returns a length-n weight vector summing to 1 over eligible."""
        cfg, n = self.cfg, self.n
        lat = np.asarray(lat, dtype=float)
        load = np.asarray(load, dtype=float)
        mask = np.asarray(mask, dtype=bool)

        # online capacity estimate: running-min latency per eligible backend.
        for i in range(n):
            if mask[i] and np.isfinite(lat[i]):
                self._minlat[i] = min(self._minlat[i], max(lat[i], cfg.cap_floor_ms))
        base = np.where(np.isfinite(self._minlat), self._minlat, lat)
        base = np.clip(base, cfg.cap_floor_ms, None)

        cap = 1.0 / base
        lat_c = np.clip(np.where(mask, lat, np.inf), 1e-3, None)
        degr = lat_c / base
        score = cap / np.power(degr, cfg.degr_pow)

        if mask.any():
            lmin = float(np.min(lat_c[mask]))
            score = np.where(mask & (lat_c > cfg.cut * lmin), score * 1e-3, score)

        target = self._normalize(score, mask, n)

        a = 1.0 if float(load.sum()) < cfg.idle_load else cfg.alpha
        if self._w is None:
            self._w = target
        else:
            self._w = self._normalize((1.0 - a) * self._w + a * target, mask, n)
        return self._w
