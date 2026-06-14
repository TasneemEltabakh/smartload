"""
experiments/rl-routing-bench/monotonicity_probe.py
───────────────────────────────────────────────────
Latency-monotonicity probe (acceptance gate).

Principle: a load balancer must never route MORE traffic to a backend BECAUSE it
got SLOWER. The probe verifies this all-else-equal: take a state, sweep ONE
backend's latency upward over a grid (holding every other backend and that
backend's load/health fixed), and require that backend's routing weight to be
NON-INCREASING in its own latency.

Coverage: homogeneous and heterogeneous base pools, several load levels, several
pool sizes / eligible counts, and a HELD-OUT regime (latencies in 50-90 ms and
degraded out to >2000 ms, disjoint from the training 12-45 ms range) so the probe
tests out-of-distribution states too. Each grid point is evaluated on a FRESH
policy instance fed only that state, so the test isolates the instantaneous
state->weight map (no history leakage).

A policy PASSES iff no sweep shows the target weight rising by more than `tol`
(default 1e-3) as the target latency increases.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[2]
_RL_ENGINE = _REPO / "services" / "rl-engine"
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))

from policy_base import BackendState  # noqa: E402

_LAT_GRID = [10, 20, 30, 45, 60, 80, 120, 200, 350, 600, 1000, 1800, 3000]


def _state(lats, loads, healths):
    return [BackendState(backend_id=f"backend_{i+1}", latency_ms=float(lats[i]),
                         queue_depth=float(loads[i]), health=healths[i])
            for i in range(len(lats))]


def _base_states():
    """Yield (label, base_lats, loads, healths, target_idx) probe scenarios."""
    cases = []
    # homogeneous pools at several load levels
    for load in (1.0, 20.0, 80.0):
        cases.append((f"homo_load{load:.0f}", [30, 30, 30, 30, 30],
                      [load] * 5, ["healthy"] * 5, 0))
    # heterogeneous base (in-distribution 12-45 ms)
    cases.append(("hetero_indist", [12, 20, 30, 40, 45], [20] * 5,
                  ["healthy"] * 5, 2))
    # held-out base (50-90 ms, disjoint from training)
    cases.append(("heldout_base", [55, 65, 72, 84, 90], [30] * 5,
                  ["healthy"] * 5, 1))
    # smaller pool (3 eligible + 2 absent/unhealthy)
    cases.append(("pool3", [25, 40, 60, 0, 0], [15, 15, 15, 0, 0],
                  ["healthy", "healthy", "healthy", "unhealthy", "unhealthy"], 1))
    # one already-degraded neighbour present
    cases.append(("with_degraded", [20, 30, 800, 25, 35], [20, 20, 5, 20, 20],
                  ["healthy", "healthy", "degraded", "healthy", "healthy"], 0))
    return cases


def run_probe(policy_builder, label="policy", tol=1e-3, verbose=False):
    """policy_builder() -> fresh callable f(state)->weight_vector (len>=n).
    Returns dict with passed, max_violation, n_sweeps, n_violations, worst."""
    max_viol = 0.0
    worst = None
    n_sweeps = 0
    n_viol = 0
    for name, base_lats, loads, healths, tgt in _base_states():
        n_sweeps += 1
        prev_w = None
        prev_lat = None
        ws = []
        for lat in _LAT_GRID:
            lats = list(base_lats)
            lats[tgt] = lat
            f = policy_builder()                # fresh instance: no history
            w = np.asarray(f(_state(lats, loads, healths)), dtype=float)
            wt = float(w[tgt])
            ws.append((lat, wt))
            if prev_w is not None:
                rise = wt - prev_w              # >0 means weight grew with latency
                if rise > max_viol:
                    max_viol = rise
                    worst = (name, prev_lat, lat, prev_w, wt)
                if rise > tol:
                    n_viol += 1
            prev_w, prev_lat = wt, lat
        if verbose:
            print(f"  [{label}] {name:<16} " +
                  " ".join(f"{l}:{wt:.3f}" for l, wt in ws))
    passed = max_viol <= tol
    return {"label": label, "passed": passed, "max_violation": max_viol,
            "n_sweeps": n_sweeps, "n_violations": n_viol, "worst": worst}


if __name__ == "__main__":
    # Self-check on a trivial monotone reference (inverse-latency) and a
    # deliberately non-monotone one.
    def inv_lat_builder():
        def f(state):
            lat = np.array([s.latency_ms for s in sorted(state, key=lambda s: s.backend_id)])
            elig = np.array([s.health in ("healthy", "degraded") for s in
                             sorted(state, key=lambda s: s.backend_id)])
            w = np.where(elig, 1.0 / np.clip(lat, 1e-3, None), 0.0)
            return w / w.sum() if w.sum() else elig / max(elig.sum(), 1)
        return f

    def bad_builder():
        def f(state):
            lat = np.array([s.latency_ms for s in sorted(state, key=lambda s: s.backend_id)])
            elig = np.array([s.health in ("healthy", "degraded") for s in
                             sorted(state, key=lambda s: s.backend_id)])
            w = np.where(elig, np.clip(lat, 1e-3, None), 0.0)   # MORE to slower (bad!)
            return w / w.sum() if w.sum() else elig / max(elig.sum(), 1)
        return f

    print("inverse-latency (should PASS):", run_probe(inv_lat_builder, "inv_lat"))
    print("route-to-slowest (should FAIL):", run_probe(bad_builder, "bad"))
