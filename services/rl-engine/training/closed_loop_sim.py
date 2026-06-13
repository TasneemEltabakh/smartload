"""
services/rl-engine/training/closed_loop_sim.py
───────────────────────────────────────────────
Causal, heterogeneous closed-loop queue simulator for routing-policy training.

WHY THIS EXISTS (contrast with simulator.py)
─────────────────────────────────────────────
simulator.py is an open-loop *bandit replay*: step(action) ignores the action,
so the agent's routing never affects what it observes and concentrating load is
free. A policy trained there learns to dump traffic on one backend — which the
closed-loop validation (experiments/.../validation-20260613) showed is ~2x worse
than round-robin once backends actually queue.

This simulator closes the loop. The agent's action is a WEIGHT VECTOR over
backends; the window's arrivals are split by those weights; and each backend's
latency is COMPUTED from an M/G/c queue given the load it received. So
concentration has a consequence (queue-wait + 503 shed) the policy can learn
from. The queue model mirrors the runtime backend
(test-backends/lib/pool.js + service_time.js): `workers` service slots, a bounded
`queue_max` FIFO, and shedding past it.

HETEROGENEITY (the "hetero not homo" requirement)
─────────────────────────────────────────────────
Heterogeneity lives in the BACKEND PROFILES, not a trace. Each episode samples a
scenario: backends with different service means, and optionally a mid-episode
degradation (one backend slows for a span) — the anomaly case. Demand (arrivals
per window) is generated synthetically (steady / ramp / burst / diurnal), so the
trainer does not depend on re-fetching the Alibaba trace. This is a curriculum:
homogeneous, statically-heterogeneous, and degrading scenarios all appear.

State parity: reset()/current state return `list[BackendState]` with the same
[latency_ms, queue_depth, health] fields obs_builder + serving consume, so the
observation representation is identical to production.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

_RL_ENGINE = Path(__file__).resolve().parents[1]
if str(_RL_ENGINE) not in sys.path:
    sys.path.insert(0, str(_RL_ENGINE))

from policy_base import BackendState     # noqa: E402
from runloop import classify_health      # noqa: E402

WINDOW_S: float = 1.0          # each step models a 1-second window
DEFAULT_EPISODE_LENGTH: int = 128


# ── queue model (analytical M/M/c with a bounded buffer) ───────────────────────

def _erlang_c(c: int, a: float) -> float:
    """Erlang-C: probability an arrival must wait, given c servers and offered
    load `a` erlangs (a = lambda * service_time). Computed via the numerically
    stable Erlang-B recursion. Returns 1.0 when a >= c (saturated)."""
    if a <= 0:
        return 0.0
    if a >= c:
        return 1.0
    b = 1.0
    for n in range(1, c + 1):
        b = (a * b) / (n + a * b)   # Erlang-B
    rho = a / c
    denom = 1.0 - rho * (1.0 - b)
    if denom <= 0:
        return 1.0
    return min(1.0, b / denom)


def queue_response(
    lam_rps: float,
    workers: int,
    service_s: float,
    queue_max: int,
) -> tuple[float, float]:
    """Return (mean_latency_ms, shed_fraction) for one backend over a window.

    Below capacity (rho < 1): mean sojourn = service + M/M/c queue-wait, capped
    at the saturated value so it stays finite near rho=1. At/above capacity the
    backend serves at its ceiling (workers/service) and sheds the overflow with
    a near-full-queue latency. This is the analytical analogue of pool.js:
    `workers` slots, bounded `queue_max`, 503 past it.
    """
    service_s = max(service_s, 1e-4)
    cap_rps = workers / service_s                       # max throughput
    sat_latency_ms = service_s * (1.0 + queue_max / workers) * 1000.0
    if lam_rps <= 0:
        return service_s * 1000.0, 0.0

    a = lam_rps * service_s                             # offered load (erlangs)
    rho = a / workers
    if rho < 1.0:
        c_wait = _erlang_c(workers, a)
        wq = c_wait / (cap_rps - lam_rps) if cap_rps > lam_rps else float("inf")
        lat_ms = (service_s + wq) * 1000.0
        return min(lat_ms, sat_latency_ms), 0.0

    # Saturated: serve at the ceiling, shed the rest, latency pinned near full queue.
    shed = 1.0 - 1.0 / rho                              # rho >= 1
    return sat_latency_ms, float(min(max(shed, 0.0), 0.99))


# ── backend profiles + scenarios ───────────────────────────────────────────────

@dataclass
class BackendProfile:
    """One backend's queue parameters. Mirrors test-backends/lib/config.js
    defaults; `extra_ms` is a runtime degradation toggled by scenario events."""
    backend_id: str
    workers: int = 2
    service_mean_ms: float = 20.0
    queue_max: int = 64
    extra_ms: float = 0.0

    @property
    def service_s(self) -> float:
        return (self.service_mean_ms + self.extra_ms) / 1000.0

    @property
    def capacity_rps(self) -> float:
        return self.workers / self.service_s


@dataclass
class Scenario:
    """A sampled episode: backend profiles, a per-window demand curve, and an
    optional degradation event (backend index, start step, span, extra_ms)."""
    profiles: list[BackendProfile]
    demand_rps: np.ndarray                 # shape (episode_length,)
    degrade: tuple[int, int, int, float] | None = None   # (idx, start, span, extra_ms)
    kind: str = "homogeneous"


def _demand_curve(rng: np.random.Generator, n: int, base_cap: float) -> np.ndarray:
    """Synthetic per-window arrival rate (rps). Pattern sampled per episode;
    scaled so peak load sits in a band that exercises both light and saturating
    regimes relative to total pool capacity `base_cap`."""
    # ~20% of episodes are near-idle / very-low-load. The policy never saw this
    # regime before and went OOD on it live (it concentrated when traffic was
    # near zero). At idle the latency reward is flat, so the spread penalty in
    # reward_v2 is what teaches "stay uniform when there's nothing to optimise".
    if rng.random() < 0.20:
        level = rng.uniform(0.01, 0.12)
        noise = rng.normal(1.0, 0.05, size=n).clip(0.5, 1.5)
        return (level * base_cap * noise).clip(min=0.0)

    pattern = rng.choice(["steady", "ramp", "burst", "diurnal"])
    t = np.arange(n)
    if pattern == "steady":
        curve = np.ones(n)
    elif pattern == "ramp":
        curve = np.linspace(0.3, 1.0, n)
    elif pattern == "burst":
        curve = np.full(n, 0.4)
        s = rng.integers(n // 4, n // 2)
        curve[s:s + n // 4] = 1.0
    else:  # diurnal
        curve = 0.6 + 0.4 * np.sin(2 * np.pi * t / n)
    # Peak utilisation band: mostly the regime where routing quality actually
    # differs (latency rises with concentration but the pool isn't fully
    # saturated), with occasional brief overload. Above ~1.0 no routing can
    # avoid shedding, so we don't want those windows to dominate the signal.
    peak_util = rng.uniform(0.4, 1.05)
    noise = rng.normal(1.0, 0.05, size=n).clip(0.7, 1.3)
    return (curve * peak_util * base_cap * noise).clip(min=0.0)


def sample_scenario(
    rng: np.random.Generator,
    n_backends: int,
    episode_length: int,
) -> Scenario:
    """Sample a curriculum episode: ~1/3 homogeneous, ~1/3 statically
    heterogeneous, ~1/3 with a mid-episode degradation (anomaly)."""
    roll = rng.random()
    profiles = [
        BackendProfile(backend_id=f"backend_{i + 1}", workers=2, service_mean_ms=20.0)
        for i in range(n_backends)
    ]
    # Curriculum tilted toward the scenarios where routing quality actually
    # differs (heterogeneous + degrading), so the adaptive signal dominates the
    # gradient rather than the homogeneous case where round-robin is already
    # optimal. ~25% homogeneous / ~35% heterogeneous / ~40% degrading.
    kind = "homogeneous"
    if roll > 0.25:
        # Static heterogeneity: each backend's base service mean varies.
        kind = "heterogeneous"
        for p in profiles:
            p.service_mean_ms = float(rng.uniform(12.0, 45.0))
    base_cap = sum(p.capacity_rps for p in profiles)
    demand = _demand_curve(rng, episode_length, base_cap)

    degrade = None
    if roll > 0.60:
        # Anomaly: one backend degrades for a span mid-episode.
        kind = "degrading"
        idx = int(rng.integers(0, n_backends))
        start = int(rng.integers(episode_length // 4, episode_length // 2))
        span = int(rng.integers(episode_length // 8, episode_length // 3))
        extra = float(rng.uniform(150.0, 400.0))
        degrade = (idx, start, span, extra)
    return Scenario(profiles=profiles, demand_rps=demand, degrade=degrade, kind=kind)


# ── the simulator ──────────────────────────────────────────────────────────────

@dataclass
class StepMetrics:
    """Per-window outcome used by the reward and by evaluation."""
    served_mean_latency_ms: float
    max_backend_latency_ms: float
    shed_fraction: float
    utilisation: np.ndarray        # per-backend rho
    routed_rps: np.ndarray         # per-backend arrival rate
    weight_hhi: float = 0.2        # Herfindahl of the routing weights (1/n..1):
                                   # 1/n = perfectly even, 1 = all on one backend.
                                   # Lets the reward prefer spreading when latency
                                   # is indifferent (e.g. at idle).


class ClosedLoopSimulator:
    """Causal queue simulator. reset() samples a scenario; step(weights) splits
    that window's demand by `weights`, runs each backend's queue, and returns
    (next_state, metrics, done)."""

    def __init__(self, n_backends: int, episode_length: int = DEFAULT_EPISODE_LENGTH):
        self.n_backends = n_backends
        self.episode_length = episode_length
        self._rng = np.random.default_rng()
        self._scn: Scenario | None = None
        self._step = 0

    def reset(self, seed: int | None = None, force_kind: str | None = None) -> list[BackendState]:
        self._rng = np.random.default_rng(seed)
        scn = sample_scenario(self._rng, self.n_backends, self.episode_length)
        if force_kind is not None:
            guard = 0
            while scn.kind != force_kind and guard < 500:
                scn = sample_scenario(self._rng, self.n_backends, self.episode_length)
                guard += 1
        self._scn = scn
        self._step = 0
        # Initial observation: idle backends at their base service latency.
        return self._state_from_metrics(
            self._eval_window(np.ones(self.n_backends) / self.n_backends, apply_step=False)
        )

    def _active_profiles(self) -> list[BackendProfile]:
        """Profiles with the current window's degradation applied."""
        scn = self._scn
        profs = [BackendProfile(**vars(p)) for p in scn.profiles]
        if scn.degrade is not None:
            idx, start, span, extra = scn.degrade
            if start <= self._step < start + span:
                profs[idx].extra_ms = extra
        return profs

    def _eval_window(self, weights: np.ndarray, apply_step: bool) -> StepMetrics:
        scn = self._scn
        profs = self._active_profiles()
        w = np.asarray(weights, dtype=float).clip(min=0.0)
        w = w / w.sum() if w.sum() > 0 else np.ones(self.n_backends) / self.n_backends
        total_rps = float(scn.demand_rps[min(self._step, self.episode_length - 1)])
        routed = w * total_rps

        lat = np.zeros(self.n_backends)
        shed = np.zeros(self.n_backends)
        util = np.zeros(self.n_backends)
        for i, p in enumerate(profs):
            lat[i], shed[i] = queue_response(routed[i], p.workers, p.service_s, p.queue_max)
            util[i] = routed[i] / p.capacity_rps if p.capacity_rps > 0 else 0.0

        served = routed * (1.0 - shed)
        served_total = served.sum()
        served_mean_lat = float((lat * served).sum() / served_total) if served_total > 0 else float(lat.mean())
        shed_frac = float((routed * shed).sum() / routed.sum()) if routed.sum() > 0 else 0.0
        return StepMetrics(
            served_mean_latency_ms=served_mean_lat,
            max_backend_latency_ms=float(lat.max()),
            shed_fraction=shed_frac,
            utilisation=util,
            routed_rps=routed,
            weight_hhi=float((w ** 2).sum()),
        )

    def step(self, weights: np.ndarray) -> tuple[list[BackendState], StepMetrics, bool]:
        metrics = self._eval_window(weights, apply_step=True)
        state = self._state_from_metrics(metrics)
        self._step += 1
        done = self._step >= self.episode_length
        return state, metrics, done

    def _state_from_metrics(self, m: StepMetrics) -> list[BackendState]:
        profs = self._active_profiles()
        states: list[BackendState] = []
        for i, p in enumerate(profs):
            lat, shed = queue_response(m.routed_rps[i], p.workers, p.service_s, p.queue_max)
            states.append(BackendState(
                backend_id=p.backend_id,
                latency_ms=lat,
                queue_depth=float(m.routed_rps[i] * WINDOW_S),   # load proxy (req/window)
                health=classify_health(lat, shed),
            ))
        return states

    @property
    def scenario_kind(self) -> str:
        return self._scn.kind if self._scn else "none"
