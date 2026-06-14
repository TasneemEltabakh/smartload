"""
services/rl-engine/policies/monotone/policy.py
───────────────────────────────────────────────
MonotonePolicy — serving plugin for the latency-monotone capacity-aware router
(candidate_mono). Stateful: one instance per serving session carries the online
capacity estimate (running-min latency) and the damped weight vector.

Loaded by select_policy("monotone"). Reads its config from a params.json artifact
(same shape train_monotone.py writes), defaulting to models/candidate_mono/.

Serving / training separation: like the other serving plugins this file imports
ONLY obs_builder and policy_base — the (small) router math is inlined here, kept
byte-for-byte equivalent to training/monotone_router.MonotoneRouter so train and
serve agree. It NEVER imports from training/.

Monotone by construction: holding history fixed, a backend's score is strictly
decreasing in its current latency; the capacity estimate depends on PAST latencies
only. So routing weight never increases with a backend's latency — the property the
benchmark's latency-monotonicity probe verifies.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parents[2]   # policies/ -> rl-engine/
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from obs_builder import N_MAX_BACKENDS                      # noqa: E402
from policy_base import (                                   # noqa: E402
    BackendState, Ranking, RoutingAction, RoutingPolicy, is_eligible,
)

_log = logging.getLogger(__name__)
_DEFAULTS = dict(degr_pow=1.0, alpha=0.4, cut=3.0, cap_floor_ms=5.0, idle_load=8.0)


class MonotonePolicy(RoutingPolicy):
    def __init__(self, operating_mode: str = "shadow", model_path: str | None = None,
                 confidence_threshold: float = 0.6, exploration_rate: float = 0.0,
                 **kwargs):
        # confidence_threshold / exploration_rate are accepted for API parity
        # with the other serving plugins; the monotone router does not consume
        # them. **kwargs absorbs any future policy-derived constructor params so
        # an evolving operating-policy payload never makes select_policy raise
        # (which would silently demote the service to the random_shadow baseline).
        self._operating_mode = "hybrid" if operating_mode in ("hybrid", "learning") else "shadow"
        if model_path is None:
            model_path = str(_HERE / "models" / "candidate_mono")
        cfg = dict(_DEFAULTS)
        meta_path = Path(model_path) / "params.json"
        try:
            cfg.update(json.loads(meta_path.read_text()).get("monotone_config", {}))
            _log.info("MonotonePolicy: loaded config from %s: %s", meta_path, cfg)
        except Exception as exc:  # noqa: BLE001
            _log.warning("MonotonePolicy: using defaults (%s): %s", meta_path, exc)
        self._cfg = cfg
        self._minlat = np.full(N_MAX_BACKENDS, np.inf)
        self._w: np.ndarray | None = None

    def reload(self, **kwargs) -> None:
        if "operating_mode" in kwargs:
            m = kwargs["operating_mode"]
            self._operating_mode = "hybrid" if m in ("hybrid", "learning") else "shadow"

    def act(self, state: list[BackendState]) -> RoutingAction:
        ss = sorted(state, key=lambda s: s.backend_id)
        n = min(len(ss), N_MAX_BACKENDS)
        if n == 0:
            return RoutingAction(mode="shadow", rankings=[])
        eligible = [s for s in ss[:n] if is_eligible(s.health)]
        if not eligible:
            return RoutingAction(mode="shadow", rankings=[])

        cfg = self._cfg
        lat = np.array([s.latency_ms for s in ss[:n]], dtype=float)
        mask = np.array([is_eligible(s.health) for s in ss[:n]], dtype=bool)

        for i in range(n):
            if mask[i] and np.isfinite(lat[i]):
                self._minlat[i] = min(self._minlat[i], max(lat[i], cfg["cap_floor_ms"]))
        base = np.where(np.isfinite(self._minlat[:n]), self._minlat[:n], lat)
        base = np.clip(base, cfg["cap_floor_ms"], None)
        cap = 1.0 / base
        lat_c = np.clip(np.where(mask, lat, np.inf), 1e-3, None)
        score = cap / np.power(lat_c / base, cfg["degr_pow"])
        lmin = float(np.min(lat_c[mask]))
        score = np.where(mask & (lat_c > cfg["cut"] * lmin), score * 1e-3, score)
        target = self._normalize(np.where(mask, score, 0.0), mask, n)

        load_total = float(sum(s.queue_depth for s in eligible))
        a = 1.0 if load_total < cfg["idle_load"] else cfg["alpha"]
        if self._w is None or len(self._w) != n:
            self._w = target
        else:
            self._w = self._normalize((1.0 - a) * self._w + a * target, mask, n)

        rankings = [Ranking(backend_id=ss[i].backend_id, score=float(self._w[i]))
                    for i in range(n) if mask[i] and self._w[i] > 0]
        mode = "active" if self._operating_mode == "hybrid" else "shadow"
        return RoutingAction(mode=mode, rankings=rankings)

    @staticmethod
    def _normalize(w, mask, n):
        w = np.where(mask, np.clip(w, 0.0, None), 0.0)
        t = w.sum()
        if t <= 0:
            w = mask.astype(float)
            return w / w.sum() if w.sum() else np.ones(n) / n
        return w / t
