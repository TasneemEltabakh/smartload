"""
experiments/autoscaler-strategy-bench/eval_harmonic.py
───────────────────────────────────────────────────────
One-off integration test: does the new `harmonic_residual` forecaster (from the
forecasting track) improve autoscaler SLA over the shipped moving-average, both
on the shipped ±1 rule and under the new target-based controller?

It is a *pluggable input* test — only the forecast SIGNAL changes; the controller
and warm-up model are the same harness used everywhere else. The harmonic engine
exposes `forecast_ahead(history, steps)`; the operationally useful lead is the
provisioning warm-up delay `w`, so the signal asks for a w-step-ahead forecast
fed the full trailing history (the engine caps it to its own fit window).

The forecasting engine is loaded from a worktree of its branch (path via
--forecasting-root) so this needs no merge and no Docker — the sim is pure-numpy.

Run:
  python experiments/autoscaler-strategy-bench/eval_harmonic.py \
      --forecasting-root /tmp/fc-forecasting/services/forecasting
"""

from __future__ import annotations

import argparse
import importlib
import pathlib
import sys

import numpy as np

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parents[1] / "experiments" / "_bench_common"))

from bench_stats import mean_ci  # noqa: E402
from demand import demand_curve, PROFILES  # noqa: E402
import sim  # noqa: E402
from sim import (  # noqa: E402
    SimParams, signal_oracle, signal_predictive, signal_reactive,
    HoltForecaster, control_policy, _run_dynamic, _run_controller, _warm_start,
)

PEAK_MULT = 8.0


def _load_harmonic(forecasting_root: str):
    """Load the harmonic engine by FILE PATH from the forecasting branch worktree.

    `engine_base` (HistoryWindow/Forecast/ForecastEngine) is already importable
    (sim imported it) and unchanged on the branch, so the engine's
    `from engine_base import ...` resolves. We just exec the new engine file.
    """
    import importlib.util
    root = pathlib.Path(forecasting_root).resolve()
    sys.path.insert(0, str(root))  # lets `from engine_base import ...` resolve
    base = importlib.import_module("engine_base")
    eng_path = root / "engines" / "harmonic_residual" / "engine.py"
    spec = importlib.util.spec_from_file_location("harmonic_engine", eng_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["harmonic_engine"] = mod
    spec.loader.exec_module(mod)
    return mod.HarmonicResidualEngine, base.HistoryWindow


# Precomputed 1-second-cadence ISO timestamps (the sim's true cadence), so the
# engine infers the correct period instead of assuming the 5-min-bucket default.
_TS_ALL: list[str] | None = None


def _timestamps(n: int) -> list[str]:
    global _TS_ALL
    if _TS_ALL is None or len(_TS_ALL) < n:
        from datetime import datetime, timedelta, timezone
        base = datetime(2019, 7, 15, tzinfo=timezone.utc)
        _TS_ALL = [(base + timedelta(seconds=i)).isoformat() for i in range(n)]
    return _TS_ALL


def signal_harmonic(demand, t, p, engine, HW, with_ts: bool = True,
                    field: str = "predicted_rps"):
    """w-ahead harmonic forecast fed the full trailing history (engine caps it).

    `with_ts=True` passes true 1-second timestamps so the engine infers the real
    cadence; False passes none (its default-period behaviour). `field` selects
    which envelope value the autoscaler sizes to: the point forecast
    (`predicted_rps`) or the conformal upper band (`confidence_upper`)."""
    rates = demand[: t + 1].tolist()
    ts = _timestamps(len(demand))[: t + 1] if with_ts else []
    hw = HW(timestamps=ts, request_rates=rates)
    return float(getattr(engine.forecast_ahead(hw, steps=p.warmup_steps), field))


def run_one(strategy, demand, p, *, seed, HarmonicCls, HW):
    """Dispatch including the two new harmonic strategies; everything else
    falls through to the shipped sim.run_strategy."""
    n0 = _warm_start(demand, p)
    if strategy == "S6_harmonic":            # shipped ±1 rule + harmonic (w/ ts)
        eng = HarmonicCls(horizon_minutes=max(1, p.horizon_steps // 60))
        return _run_dynamic(demand, p,
                            lambda t: signal_harmonic(demand, t, p, eng, HW, True),
                            cooldown=p.cooldown_seconds, start_count=n0)
    if strategy == "C7_ctrl_harmonic":       # new controller + harmonic (w/ ts)
        eng = HarmonicCls(horizon_minutes=max(1, p.horizon_steps // 60))
        cpol = control_policy(p, cooldown=p.cooldown_seconds)
        return _run_controller(demand, p,
                               lambda t: signal_harmonic(demand, t, p, eng, HW, True),
                               cpol, start_count=n0)
    if strategy == "C7n_ctrl_harmonic_nots":  # controller + harmonic, NO timestamps
        eng = HarmonicCls(horizon_minutes=max(1, p.horizon_steps // 60))
        cpol = control_policy(p, cooldown=p.cooldown_seconds)
        return _run_controller(demand, p,
                               lambda t: signal_harmonic(demand, t, p, eng, HW, False),
                               cpol, start_count=n0)
    if strategy == "C8_ctrl_harmonic_upper":  # controller + harmonic UPPER band
        eng = HarmonicCls(horizon_minutes=max(1, p.horizon_steps // 60))
        # No controller headroom: the forecaster's own conformal band supplies it.
        cpol = control_policy(p, cooldown=p.cooldown_seconds, headroom=0.0)
        return _run_controller(
            demand, p,
            lambda t: signal_harmonic(demand, t, p, eng, HW, True, "confidence_upper"),
            cpol, start_count=n0)
    if strategy == "C9_ctrl_harmonic_local":   # SHORT fit window → local trend
        eng = HarmonicCls(horizon_minutes=max(1, p.horizon_steps // 60),
                          fit_window=120)
        cpol = control_policy(p, cooldown=p.cooldown_seconds)
        return _run_controller(demand, p,
                               lambda t: signal_harmonic(demand, t, p, eng, HW, True),
                               cpol, start_count=n0)
    if strategy == "C10_ctrl_harmonic_norobust":  # IRLS off → don't smooth spikes
        eng = HarmonicCls(horizon_minutes=max(1, p.horizon_steps // 60),
                          robust_mode="downward")
        cpol = control_policy(p, cooldown=p.cooldown_seconds)
        return _run_controller(demand, p,
                               lambda t: signal_harmonic(demand, t, p, eng, HW, True),
                               cpol, start_count=n0)
    if strategy == "C11_ctrl_harmonic_local_norobust":  # both fixes together
        eng = HarmonicCls(horizon_minutes=max(1, p.horizon_steps // 60),
                          fit_window=120, robust_mode="downward")
        cpol = control_policy(p, cooldown=p.cooldown_seconds)
        return _run_controller(demand, p,
                               lambda t: signal_harmonic(demand, t, p, eng, HW, True),
                               cpol, start_count=n0)
    return sim.run_strategy(strategy, demand, p,
                            cooldown=p.cooldown_seconds, seed=seed)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--forecasting-root", required=True)
    ap.add_argument("--seeds", type=int, default=8)
    ap.add_argument("--seed0", type=int, default=1000)
    ap.add_argument("--real", action="store_true",
                    help="use the real-trace demand sources instead of synthetic")
    args = ap.parse_args(argv)

    HarmonicCls, HW = _load_harmonic(args.forecasting_root)
    seeds = list(range(args.seed0, args.seed0 + args.seeds))
    p = SimParams()
    peak = PEAK_MULT * p.per_instance_capacity_rps

    if args.real:
        from realtrace import realtrace_curve, REAL_SOURCES
        global PROFILES
        PROFILES = REAL_SOURCES
        _curve = lambda prof, seed: realtrace_curve(prof, p.run_steps, peak, seed)
    else:
        _curve = lambda prof, seed: demand_curve(prof, p.run_steps, peak, seed)

    strategies = [
        ("S2_predictive",    "S2  shipped ±1 rule + MA forecast (BASELINE)"),
        ("C2_ctrl_predictive","C2  controller + MA forecast"),
        ("C4_ctrl_trend",    "C4  controller + trend (Holt) forecast"),
        ("C7_ctrl_harmonic", "C7  controller + HARMONIC point (default)"),
        ("C9_ctrl_harmonic_local", "C9  + SHORT fit window (local trend)"),
        ("C10_ctrl_harmonic_norobust", "C10 + IRLS off (keep spikes)"),
        ("C11_ctrl_harmonic_local_norobust", "C11 + both fixes"),
        ("C1_ctrl_oracle",   "C1  controller + oracle (ceiling)"),
    ]

    # results[strat][metric][profile] = list over seeds
    res: dict = {s: {"sla_pct": {}, "overprov_cost": {}, "scale_actions": {}}
                 for s, _ in strategies}
    for profile in PROFILES:
        for s, _ in strategies:
            for m in res[s]:
                res[s][m].setdefault(profile, [])
        for seed in seeds:
            d = _curve(profile, seed)
            for s, _ in strategies:
                r = run_one(s, d, p, seed=seed, HarmonicCls=HarmonicCls, HW=HW)
                res[s]["sla_pct"][profile].append(r.sla_pct)
                res[s]["overprov_cost"][profile].append(r.overprov_cost)
                res[s]["scale_actions"][profile].append(r.scale_actions)

    def agg(s, m):
        vals = [v for prof in PROFILES for v in res[s][m][prof]]
        return mean_ci(vals)

    print(f"\nSynthetic grid: {len(PROFILES)} profiles × {len(seeds)} seeds "
          f"(warm-up {p.warmup_steps}s, cooldown {p.cooldown_seconds:.0f}s)\n")
    print(f"{'Strategy':48s} {'SLA%':>14s} {'Over-prov':>10s} {'#Acts':>7s}")
    print("-" * 82)
    for s, label in strategies:
        sla = agg(s, "sla_pct"); cost = agg(s, "overprov_cost"); act = agg(s, "scale_actions")
        print(f"{label:48s} {sla['mean']:6.1f}±{sla['half_width']:4.1f}  "
              f"{cost['mean']:9.0f} {act['mean']:7.1f}")

    print("\nPer-profile SLA% (harmonic default vs the two fixes):")
    print(f"{'profile':10s} {'C7 def':>8s} {'C9 local':>9s} {'C10 norob':>10s} "
          f"{'C11 both':>9s} {'C4 trend':>9s}")
    for prof in PROFILES:
        def pm(s): return float(np.mean(res[s]['sla_pct'][prof]))
        print(f"{prof:10s} {pm('C7_ctrl_harmonic'):8.1f} "
              f"{pm('C9_ctrl_harmonic_local'):9.1f} "
              f"{pm('C10_ctrl_harmonic_norobust'):10.1f} "
              f"{pm('C11_ctrl_harmonic_local_norobust'):9.1f} {pm('C4_ctrl_trend'):9.1f}")

    print("\nAggregate SLA% under the controller:")
    for s, lab in [("C2_ctrl_predictive","MA point"),("C4_ctrl_trend","trend (Holt)"),
                   ("C7_ctrl_harmonic","harmonic default"),
                   ("C9_ctrl_harmonic_local","harmonic + short window"),
                   ("C10_ctrl_harmonic_norobust","harmonic + IRLS off"),
                   ("C11_ctrl_harmonic_local_norobust","harmonic + both fixes")]:
        a = agg(s,"sla_pct"); print(f"  {lab:32s} {a['mean']:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
