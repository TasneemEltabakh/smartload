"""
experiments/rl-routing-bench/scenarios.py
────────────────────────────────────────────
Scenario drivers for the routing benchmark.

Four CURRICULUM kinds are produced directly by the frozen simulator via
ClosedLoopSimulator.reset(force_kind=...): homogeneous, heterogeneous,
degrading. The fourth curriculum kind, near-idle, is a demand regime
(_demand_curve draws a near-idle level ~20% of episodes) rather than a
force_kind; we realise it deterministically here by sampling scenarios and
keeping only those whose demand stays in the idle band.

The HELD-OUT family the models never trained on:
  "held_out_dual_degrade" — service means drawn from a DISJOINT, higher range
  (50-90 ms; training heterogeneous used 12-45 ms) AND two backends degrade
  simultaneously for an overlapping span (training only ever degraded ONE
  backend). Implemented with a harness-local subclass that applies a LIST of
  degradations; the queue model, demand curve and state representation are the
  frozen sim's — only scenario construction changes.

Everything reuses the frozen closed_loop_sim primitives (queue_response,
BackendProfile, _demand_curve, _state_from_metrics, _eval_window). No file under
services/ is modified.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[2]
_RL_ENGINE = _REPO / "services" / "rl-engine"
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))

from policy_base import BackendState               # noqa: E402
from training.closed_loop_sim import (             # noqa: E402
    ClosedLoopSimulator,
    BackendProfile,
    Scenario,
    _demand_curve,
)

CURRICULUM_KINDS = ["homogeneous", "heterogeneous", "degrading", "near-idle"]
HELD_OUT_KIND = "held_out_dual_degrade"
ALL_KINDS = CURRICULUM_KINDS + [HELD_OUT_KIND]

# Idle band: per-window demand below this fraction of total pool capacity counts
# as near-idle. _demand_curve draws the idle regime at level uniform(0.01, 0.12);
# we accept episodes whose MEAN utilisation stays under this threshold.
_IDLE_UTIL_MAX = 0.15


class HeldOutSimulator(ClosedLoopSimulator):
    """Closed-loop sim whose reset() builds the held-out dual-degrade family.

    Differs from the trained curriculum in two independent ways the models never
    saw together:
      • base service means in [50, 90] ms (disjoint from training's [12, 45]);
      • TWO backends degrade simultaneously over an overlapping mid-episode span.

    Reuses the parent queue model, demand curve and state builder verbatim; only
    scenario construction and the per-window degradation application change.
    """

    def __init__(self, n_backends: int, episode_length: int = 128):
        super().__init__(n_backends, episode_length=episode_length)
        self._degrades: list[tuple[int, int, int, float]] = []

    def reset(self, seed: int | None = None, force_kind: str | None = None) -> list[BackendState]:
        rng = np.random.default_rng(seed)
        n = self.n_backends
        profiles = [
            BackendProfile(backend_id=f"backend_{i + 1}", workers=2,
                           service_mean_ms=float(rng.uniform(50.0, 90.0)))
            for i in range(n)
        ]
        base_cap = sum(p.capacity_rps for p in profiles)
        demand = _demand_curve(rng, self.episode_length, base_cap)

        # Two distinct backends degrade over overlapping spans mid-episode.
        idxs = rng.choice(n, size=2, replace=False)
        degrades: list[tuple[int, int, int, float]] = []
        for idx in idxs:
            start = int(rng.integers(self.episode_length // 4, self.episode_length // 2))
            span = int(rng.integers(self.episode_length // 8, self.episode_length // 3))
            extra = float(rng.uniform(150.0, 400.0))
            degrades.append((int(idx), start, span, extra))

        self._scn = Scenario(profiles=profiles, demand_rps=demand,
                             degrade=None, kind=HELD_OUT_KIND)
        self._degrades = degrades
        self._step = 0
        self._rng = rng
        return self._state_from_metrics(
            self._eval_window(np.ones(n) / n, apply_step=False)
        )

    def _active_profiles(self) -> list[BackendProfile]:
        """Apply the LIST of degradations (parent supports only a single one)."""
        scn = self._scn
        profs = [BackendProfile(**vars(p)) for p in scn.profiles]
        for idx, start, span, extra in self._degrades:
            if start <= self._step < start + span:
                profs[idx].extra_ms = max(profs[idx].extra_ms, extra)
        return profs


def make_sim(kind: str, n_backends: int, episode_length: int):
    """Return a simulator instance for `kind`. Held-out uses the subclass; every
    curriculum kind uses the frozen ClosedLoopSimulator."""
    if kind == HELD_OUT_KIND:
        return HeldOutSimulator(n_backends, episode_length=episode_length)
    return ClosedLoopSimulator(n_backends, episode_length=episode_length)


def reset_for_kind(sim, kind: str, seed: int, n_backends: int) -> list[BackendState]:
    """Deterministically reset `sim` into the requested scenario kind.

    homogeneous / heterogeneous / degrading: force_kind on the frozen sim.
    near-idle: rejection-sample seeds (offset deterministically) until the
      episode's mean utilisation lands in the idle band — the regime
      _demand_curve produces ~20% of the time.
    held_out_dual_degrade: HeldOutSimulator builds it directly.
    """
    if kind == HELD_OUT_KIND:
        return sim.reset(seed=seed)
    if kind in ("homogeneous", "heterogeneous", "degrading"):
        return sim.reset(seed=seed, force_kind=kind)
    if kind == "near-idle":
        # Deterministic search: try seed, seed+1e6, seed+2e6 ... until the demand
        # curve is in the idle band. Bounded so it always terminates.
        for k in range(400):
            trial = seed + k * 1_000_003           # large stride, deterministic
            state = sim.reset(seed=trial)
            scn = sim._scn
            cap = sum(p.capacity_rps for p in scn.profiles)
            mean_util = float(np.mean(scn.demand_rps)) / cap if cap > 0 else 0.0
            if mean_util <= _IDLE_UTIL_MAX:
                return state
        # Fallback: accept whatever the last reset produced (rare).
        return state
    raise ValueError(f"unknown scenario kind: {kind!r}")
