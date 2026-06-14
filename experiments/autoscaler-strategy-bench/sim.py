"""
experiments/autoscaler-strategy-bench/sim.py
─────────────────────────────────────────────
Discrete-time provisioning simulator + the five scaling strategies.

WHY A WARM-UP DELAY IS THE CENTRE OF THIS SIM
─────────────────────────────────────────────
A scale-out decision at step t does not produce serving capacity at t. A new
backend has to be provisioned and pass a health check first — model that as a
fixed warm-up delay `w`: capacity from a scale-out at t lands at t+w. Scale-in
is immediate (you can drain a backend now). Without `w`, predictive scaling has
no advantage: reacting to demand the instant it arrives would be free, so SLA
would be fiction. With `w`, acting *before* the demand (predictive) is the only
way to have capacity in place when the load lands — which is exactly the
property the benchmark is built to measure.

DECISION RULE
─────────────
All five strategies call the SHIPPED rule, services/autoscaler/decisions.py::
decide(), so what varies between them is only the SIGNAL fed in (predicted_rps)
plus two non-predictive baselines (static-N, naive-threshold). The rule's own
cooldown/bounds/guard logic is exercised identically by every strategy.

COOLDOWN
────────
decide() takes `seconds_since_last_action`. We track per-run `last_action_time`
(the sim step of the last non-NOOP action) and feed elapsed seconds (1 s/step).
None until the first action — matching the production "fresh boot, no cooldown"
semantics.
"""

from __future__ import annotations

import importlib
import importlib.util
import pathlib
import sys
from dataclasses import dataclass

import numpy as np

_REPO = pathlib.Path(__file__).resolve().parents[2]


# ── load the shipped decision rule (importlib, no package install) ─────────────

def _load_decisions():
    mod_path = _REPO / "services" / "autoscaler" / "decisions.py"
    spec = importlib.util.spec_from_file_location("autoscaler_decisions", mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["autoscaler_decisions"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_forecaster_cls():
    fc_root = _REPO / "services" / "forecasting"
    if str(fc_root) not in sys.path:
        sys.path.insert(0, str(fc_root))
    eng = importlib.import_module("engines.moving_average.engine")
    base = importlib.import_module("engine_base")
    return eng.MovingAverageEngine, base.HistoryWindow


decisions = _load_decisions()
MovingAverageEngine, HistoryWindow = _load_forecaster_cls()


# ── policy params (canonical, from config/policy.yaml) ─────────────────────────

@dataclass(frozen=True)
class SimParams:
    per_instance_capacity_rps: float = 100.0
    cooldown_seconds: float = 60.0
    min_backends: int = 1
    max_backends: int = 10
    run_steps: int = 1800            # demand timeline length (30 min × 60 s)
    horizon_steps: int = 300         # forecaster's nominal output horizon (5 min)
    warmup_steps: int = 20           # provisioning warm-up delay w (seconds)
    forecast_window_samples: int = 60
    reactive_window_steps: int = 60  # trailing observed window for S3

    def policy(self, cooldown: float | None = None) -> "decisions.Policy":
        return decisions.Policy(
            min_backends=self.min_backends,
            max_backends=self.max_backends,
            per_instance_capacity_rps=self.per_instance_capacity_rps,
            cooldown_seconds=self.cooldown_seconds if cooldown is None else cooldown,
        )


# ── signal functions (what each strategy feeds to decide() as predicted_rps) ───

def signal_oracle(demand: np.ndarray, t: int, p: SimParams) -> float:
    """S1: perfect foresight of the demand a scale-out issued NOW must cover by
    the time it finishes warming up. Upper-bound reference (oracle ceiling).

    The operationally meaningful lead time is the warm-up delay `w`: a decision at
    t produces serving capacity at t+w. A perfect-foresight scaler provisions for
    the *sustained* demand it knows is coming over that lead window, not a single
    noisy future sample — so the oracle signal is the peak demand over [t, t+w].
    Using the peak (rather than the point value at t+w) means capacity is in place
    before the load lands and is not whipsawed by per-step noise; this is exactly
    the advantage the warm-up model exists to let foresight exploit, so the oracle
    measures its ceiling. Looking the full 5-min forecast horizon ahead would
    instead over-provision minutes early, which is not what perfect foresight buys.
    """
    j = min(t + p.warmup_steps + 1, len(demand))
    return float(np.max(demand[t:j]))


def signal_predictive(demand: np.ndarray, t: int, p: SimParams,
                      engine: MovingAverageEngine) -> float:
    """S2: real moving-average forecaster over a sliding window of past demand.

    Feeds the forecaster the observed demand up to and including t. The MA engine
    averages the trailing `window_samples` — which structurally lags ramps and
    undershoots spikes. That lag is the forecast error the S1→S2 gap measures.
    """
    lo = max(0, t - p.forecast_window_samples + 1)
    rates = demand[lo:t + 1].tolist()
    hw = HistoryWindow(timestamps=[], request_rates=rates)
    return float(engine.forecast(hw).predicted_rps)


def signal_reactive(demand: np.ndarray, t: int, p: SimParams) -> float:
    """S3: mean of the trailing observed window [t-60s .. t]. Production
    reactive-fallback signal — purely backward-looking."""
    lo = max(0, t - p.reactive_window_steps + 1)
    return float(np.mean(demand[lo:t + 1]))


# ── the provisioning loop ──────────────────────────────────────────────────────

@dataclass
class RunResult:
    sla_pct: float          # fraction of steps with capacity >= demand
    unmet_rps: float        # Σ max(0, demand - capacity)
    overprov_cost: float    # Σ max(0, instances - ceil(demand/cap)) instance-seconds
    scale_actions: int      # count of non-NOOP decisions
    settling_s: float       # mean settling time after step-changes (NaN if none)
    instances_trace: np.ndarray
    capacity_trace: np.ndarray


def _settling_time(demand: np.ndarray, capacity: np.ndarray, cap_per: float,
                  threshold_frac: float = 0.25) -> float:
    """Mean steps from a demand step-change until capacity catches up and stays.

    A 'step-change' is a step where demand jumps by > threshold_frac × per-instance
    capacity vs the previous step (a jump big enough to plausibly need a new
    backend). Settling = steps until capacity >= demand and remains so through the
    next step-change (or end of run). Returns NaN if there are no step-changes.
    """
    n = len(demand)
    jumps = []
    for t in range(1, n):
        if demand[t] - demand[t - 1] > threshold_frac * cap_per:
            jumps.append(t)
    if not jumps:
        return float("nan")
    settle_times = []
    for k, t0 in enumerate(jumps):
        next_jump = jumps[k + 1] if k + 1 < len(jumps) else n
        settled = None
        for t in range(t0, next_jump):
            if capacity[t] >= demand[t]:
                settled = t - t0
                break
        # If never caught up before the next jump / end, charge the full span.
        settle_times.append(settled if settled is not None else (next_jump - t0))
    return float(np.mean(settle_times)) if settle_times else float("nan")


def _run_dynamic(demand: np.ndarray, p: SimParams, signal_fn, *,
                cooldown: float, start_count: int = 1) -> RunResult:
    """Replay `demand` under a dynamic strategy whose per-step signal is
    `signal_fn(t) -> predicted_rps`, fed to the shipped decide()."""
    n = len(demand)
    cap_per = p.per_instance_capacity_rps
    policy = p.policy(cooldown=cooldown)

    current = start_count                  # provisioned-and-serving instance count
    pending = []                           # (land_step, +1) scale-outs awaiting warm-up
    last_action_t: int | None = None
    scale_actions = 0

    instances = np.zeros(n)
    capacity = np.zeros(n)

    for t in range(n):
        # Land any warmed-up scale-outs scheduled for this step.
        if pending:
            still = []
            for land_t, delta in pending:
                if land_t <= t:
                    current = min(current + delta, p.max_backends)
                else:
                    still.append((land_t, delta))
            pending = still

        # Effective serving capacity this step.
        capacity[t] = current * cap_per
        instances[t] = current

        ssla = None if last_action_t is None else float(t - last_action_t)
        # The "in-flight" count the rule reasons about: serving + warming up.
        # Using serving-only would let the rule re-issue scale-outs every step
        # during warm-up (the production autoscaler counts pending containers).
        effective_count = current + sum(d for _, d in pending)
        effective_count = min(effective_count, p.max_backends)

        pred = float(signal_fn(t))
        dec = decisions.decide(
            predicted_rps=pred,
            current_count=effective_count,
            policy=policy,
            seconds_since_last_action=ssla,
        )

        if dec.action == decisions.ACTION_SCALE_OUT:
            pending.append((t + p.warmup_steps, +1))
            last_action_t = t
            scale_actions += 1
        elif dec.action == decisions.ACTION_SCALE_IN:
            current = max(current - 1, p.min_backends)
            last_action_t = t
            scale_actions += 1

    return _finalize(demand, instances, capacity, cap_per, scale_actions)


def _run_naive(demand: np.ndarray, p: SimParams, *, cooldown: float,
              start_count: int = 1) -> RunResult:
    """S5: threshold strategy. Scale out when observed util > 0.8, scale in when
    < 0.3, step 1, same cooldown/bounds/warm-up. The observed signal is the
    current step's demand against current serving capacity (reactive by nature)."""
    n = len(demand)
    cap_per = p.per_instance_capacity_rps
    current = start_count
    pending = []
    last_action_t: int | None = None
    scale_actions = 0
    instances = np.zeros(n)
    capacity = np.zeros(n)

    for t in range(n):
        if pending:
            still = []
            for land_t, delta in pending:
                if land_t <= t:
                    current = min(current + delta, p.max_backends)
                else:
                    still.append((land_t, delta))
            pending = still

        capacity[t] = current * cap_per
        instances[t] = current
        effective_count = min(current + sum(d for _, d in pending), p.max_backends)
        serving_cap = effective_count * cap_per
        util = demand[t] / serving_cap if serving_cap > 0 else float("inf")

        in_cooldown = (last_action_t is not None
                       and (t - last_action_t) < cooldown)

        if not in_cooldown:
            if util > 0.8 and effective_count < p.max_backends:
                pending.append((t + p.warmup_steps, +1))
                last_action_t = t
                scale_actions += 1
            elif util < 0.3 and current > p.min_backends:
                current = max(current - 1, p.min_backends)
                last_action_t = t
                scale_actions += 1

    return _finalize(demand, instances, capacity, cap_per, scale_actions)


def _run_static(demand: np.ndarray, p: SimParams, n_fixed: int) -> RunResult:
    """S4: fixed instance count, no scaling actions."""
    n = len(demand)
    cap_per = p.per_instance_capacity_rps
    n_fixed = int(np.clip(n_fixed, p.min_backends, p.max_backends))
    instances = np.full(n, float(n_fixed))
    capacity = np.full(n, float(n_fixed) * cap_per)
    return _finalize(demand, instances, capacity, cap_per, scale_actions=0)


def _finalize(demand, instances, capacity, cap_per, scale_actions) -> RunResult:
    met = capacity >= demand
    sla_pct = 100.0 * float(np.mean(met))
    unmet = float(np.sum(np.maximum(0.0, demand - capacity)))
    need = np.ceil(demand / cap_per)
    overprov = float(np.sum(np.maximum(0.0, instances - need)))
    settling = _settling_time(demand, capacity, cap_per)
    return RunResult(
        sla_pct=sla_pct,
        unmet_rps=unmet,
        overprov_cost=overprov,
        scale_actions=int(scale_actions),
        settling_s=settling,
        instances_trace=instances,
        capacity_trace=capacity,
    )


# ── strategy dispatch ──────────────────────────────────────────────────────────

def _warm_start(demand: np.ndarray, p: SimParams) -> int:
    """Initial instance count for the dynamic strategies: the cost-matched count
    ceil(mean_demand / capacity), clamped to bounds.

    The pool starts warm rather than cold at 1 so the benchmark measures
    *tracking quality* — how well each signal keeps capacity aligned with a
    moving demand curve — rather than the cold-start ramp, which is identical for
    every dynamic strategy (all rise one instance per cooldown window) and would
    otherwise wash out the signal of interest.
    """
    n0 = int(np.ceil(float(np.mean(demand)) / p.per_instance_capacity_rps))
    return int(np.clip(n0, p.min_backends, p.max_backends))


def run_strategy(strategy: str, demand: np.ndarray, p: SimParams, *,
                 cooldown: float | None = None) -> RunResult:
    """Run one named strategy on one demand realization. `strategy` is one of:
    S1_oracle, S2_predictive, S3_reactive, S4_static_max, S4_static_matched,
    S5_naive. Returns a RunResult."""
    cd = p.cooldown_seconds if cooldown is None else cooldown
    n0 = _warm_start(demand, p)

    if strategy == "S1_oracle":
        return _run_dynamic(demand, p, lambda t: signal_oracle(demand, t, p),
                            cooldown=cd, start_count=n0)
    if strategy == "S2_predictive":
        engine = MovingAverageEngine(
            horizon_minutes=max(1, int(p.horizon_steps // 60)),
            window_samples=p.forecast_window_samples,
        )
        return _run_dynamic(demand, p,
                            lambda t: signal_predictive(demand, t, p, engine),
                            cooldown=cd, start_count=n0)
    if strategy == "S3_reactive":
        return _run_dynamic(demand, p, lambda t: signal_reactive(demand, t, p),
                            cooldown=cd, start_count=n0)
    if strategy == "S4_static_max":
        return _run_static(demand, p, p.max_backends)
    if strategy == "S4_static_matched":
        # cost-matched: ceil(mean_demand / per-instance capacity)
        n_match = int(np.ceil(float(np.mean(demand)) / p.per_instance_capacity_rps))
        return _run_static(demand, p, n_match)
    if strategy == "S5_naive":
        return _run_naive(demand, p, cooldown=cd, start_count=n0)
    raise ValueError(f"unknown strategy: {strategy!r}")


STRATEGIES: tuple[str, ...] = (
    "S1_oracle",
    "S2_predictive",
    "S3_reactive",
    "S4_static_max",
    "S4_static_matched",
    "S5_naive",
)

STRATEGY_LABELS: dict[str, str] = {
    "S1_oracle": "S1 Predictive-oracle (upper bound)",
    "S2_predictive": "S2 Predictive-realistic (MA forecast)",
    "S3_reactive": "S3 Reactive (trailing mean)",
    "S4_static_max": "S4 Static N=max (SLA-optimal)",
    "S4_static_matched": "S4 Static N=cost-matched",
    "S5_naive": "S5 Naive-threshold",
}
