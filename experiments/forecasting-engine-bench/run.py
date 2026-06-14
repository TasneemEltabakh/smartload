"""
experiments/forecasting-engine-bench/run.py
─────────────────────────────────────────────
Forecasting-engine benchmark for SmartLoad.

Compares three single-step (1-bucket) forecasters on synthetic 5-minute RPS
series under a rolling-origin / walk-forward protocol:

  naive          predict next = last observed value (persistence). The honest
                 floor. Implemented locally here — NOT added to the shipped
                 service. No confidence band, so CI-coverage is n/a.

  moving_average services/forecasting/engines/moving_average/engine.py
                 (window = 60). Mean of the last 60 samples ± one stddev.

  arima_serving  services/forecasting/engines/arima/engine.py — the production
                 serving path: loads the pre-trained ARIMA(2,0,2) artifact,
                 appends the last 60 samples with refit=False, forecasts one
                 step. This is exactly what runs in prod.

All three consume the same engine_base.HistoryWindow → Forecast contract and
are handed the IDENTICAL history window at every origin, so any difference is
the model, not the data.

Protocol
────────
For each (profile, seed) series: hold out the last HOLDOUT_FRAC of the series.
Walk the origin t across the holdout region; at each t build the history window
from all samples before t, call engine.forecast(history), and record predicted
vs truth = series[t]. Slide t += 1. Identical window for every engine.

Metrics (per engine × profile, mean ± CI over seeds)
────────────────────────────────────────────────────
  MAPE   mean abs pct error, masking truth <= 0 (matches the training-time
         definition). Primary headline — SOT KPI is MAPE < 20%.
  sMAPE  symmetric MAPE — the honesty check MAPE alone can hide.
  RMSE   root mean squared error (RPS units).
  MAE    mean abs error (RPS units).
  CI-coverage  fraction of steps with lower <= truth <= upper. Target ≈ 0.95
         for the 95% band. naive has no band → n/a.
  latency_ms   wall-clock per forecast() call.

All metric accumulation is finite-masked: a single NaN prediction or truth
drops that one step, it never voids the run.

Outputs (under results/<tag>/)
──────────────────────────────
  grid.csv    one row per (profile, seed, engine) with every metric.
  SUMMARY.md  thesis-ready tables: one per profile + an overall roll-up,
              mean ± CI over seeds, with the MAPE<20% gate marked per row.
  meta.json   versions + seeds + generator params for reproducibility.

Usage
─────
    python experiments/forecasting-engine-bench/run.py
    python experiments/forecasting-engine-bench/run.py --seeds 1 2 3 --days 2
    python experiments/forecasting-engine-bench/run.py --tag my-run --engines naive moving_average

Deterministic: same args → same numbers (latency aside).
"""

from __future__ import annotations

import argparse
import csv
import json
import platform
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
_EXPERIMENTS = _REPO / "experiments"
_FORECAST_SVC = _REPO / "services" / "forecasting"

# Make the shared bench stats and the synthetic generators importable.
for _p in (str(_EXPERIMENTS), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# The forecasting engine modules resolve engine_base via the service root being
# on sys.path; mirror that here so we exercise the real shipped engines.
for _p in (
    str(_FORECAST_SVC),
    str(_FORECAST_SVC / "engines" / "moving_average"),
    str(_FORECAST_SVC / "engines" / "arima"),
    str(_FORECAST_SVC / "engines" / "harmonic_residual"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _bench_common import bench_stats  # noqa: E402
import generators  # noqa: E402
from generators import GenParams  # noqa: E402

# Pulled from the engine contract module (services/forecasting/engine_base.py).
from engine_base import Forecast, HistoryWindow  # noqa: E402


# ── Defaults ────────────────────────────────────────────────────────────────
DEFAULT_SEEDS = (1, 2, 3, 4, 5)
DEFAULT_DAYS = 3                 # series span; 3 days → multiple diurnal cycles
HOLDOUT_FRAC = 0.15              # last 15% of each series is the evaluation set
MA_WINDOW = 60                   # moving_average window (matches shipped default)
ARIMA_ORDER = (2, 0, 2)          # the artifact's order, recorded in the footer
BUCKET = "5min"
HORIZON_STEPS = 1
MAPE_GATE = 20.0                 # SOT KPI: MAPE < 20%

# Engine names in stable table order. The candidate (harmonic_residual) is last
# — it is the headline contender measured against the naive floor.
ENGINE_ORDER = ("naive", "moving_average", "arima_serving", "harmonic_residual")

# Metric columns in stable order for the CSV and the per-seed records.
METRIC_KEYS = ("mape", "smape", "rmse", "mae", "ci_coverage", "latency_ms")


# ── Naive / persistence forecaster (local — not shipped) ─────────────────────
class NaiveEngine:
    """Persistence forecaster: next = last finite observation.

    The honest floor. Deliberately produces no confidence band (lower==upper==
    the point), so CI-coverage is reported as n/a rather than a degenerate 0/1.
    Kept local to the benchmark; never added to the shipped service.
    """

    def __init__(self, horizon_minutes: int = 5) -> None:
        self.horizon_minutes = horizon_minutes

    def forecast(self, history: HistoryWindow) -> Forecast:
        finite = [r for r in history.request_rates if np.isfinite(r)]
        last = finite[-1] if finite else 0.0
        # lower == upper signals "no band" to the metric layer below.
        return Forecast(self.horizon_minutes, last, last, last)


def _load_module_by_path(name: str, path: Path):
    """Import a module from an explicit file path under a unique name.

    Both shipped engines live in files named ``engine.py``; importing either by
    bare module name is ambiguous (whichever directory sits first on sys.path
    wins). Loading by path under a distinct module name sidesteps the clash so
    we can hold both at once.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_engines(selected: tuple[str, ...]) -> dict[str, object]:
    """Instantiate the requested engines. arima_serving loads the real .pkl."""
    built: dict[str, object] = {}
    if "naive" in selected:
        built["naive"] = NaiveEngine(horizon_minutes=5)
    if "moving_average" in selected:
        ma_path = _FORECAST_SVC / "engines" / "moving_average" / "engine.py"
        ma_mod = _load_module_by_path("_ma_engine", ma_path)
        built["moving_average"] = ma_mod.MovingAverageEngine(
            horizon_minutes=5, window_samples=MA_WINDOW
        )
    if "arima_serving" in selected:
        arima_path = _FORECAST_SVC / "engines" / "arima" / "engine.py"
        arima_mod = _load_module_by_path("_arima_engine", arima_path)
        eng = arima_mod.ArimaEngine(horizon_minutes=5)
        if not getattr(eng, "model_loaded", False):
            raise RuntimeError(
                f"ARIMA artifact failed to load from {_FORECAST_SVC / 'models' / 'arima_model.pkl'} "
                "— the arima_serving contender cannot run without it."
            )
        built["arima_serving"] = eng
    if "harmonic_residual" in selected:
        hr_path = _FORECAST_SVC / "engines" / "harmonic_residual" / "engine.py"
        hr_mod = _load_module_by_path("_harmonic_residual_engine", hr_path)
        built["harmonic_residual"] = hr_mod.HarmonicResidualEngine(horizon_minutes=5)
    return built


# ── Metric helpers (all finite-masked) ───────────────────────────────────────
def _metrics(preds: np.ndarray, truth: np.ndarray,
             lowers: np.ndarray, uppers: np.ndarray,
             has_band: bool) -> dict[str, float]:
    """Compute the metric set over aligned arrays, dropping non-finite steps.

    A step is kept only if its prediction and truth are both finite. CI-coverage
    additionally requires finite band edges. Returns NaN for a metric when no
    step survives its mask, so an empty/degenerate run reads as missing rather
    than as a fake zero.
    """
    valid = np.isfinite(preds) & np.isfinite(truth)
    p = preds[valid]
    y = truth[valid]
    out: dict[str, float] = {k: float("nan") for k in METRIC_KEYS}
    if p.size == 0:
        return out

    err = p - y
    out["rmse"] = float(np.sqrt(np.mean(err ** 2)))
    out["mae"] = float(np.mean(np.abs(err)))

    # MAPE — mask truth <= 0 (the training-time definition divides by actual).
    pos = y > 0
    if pos.any():
        out["mape"] = float(np.mean(np.abs(err[pos] / y[pos])) * 100.0)

    # sMAPE — symmetric; denominator (|p|+|y|)/2, masked where that is 0.
    denom = (np.abs(p) + np.abs(y)) / 2.0
    nz = denom > 0
    if nz.any():
        out["smape"] = float(np.mean(np.abs(err[nz]) / denom[nz]) * 100.0)

    # CI-coverage — only meaningful for engines that emit a real band.
    if has_band:
        cov_valid = valid & np.isfinite(lowers) & np.isfinite(uppers)
        lo = lowers[cov_valid]
        hi = uppers[cov_valid]
        yt = truth[cov_valid]
        if yt.size:
            inside = (lo <= yt) & (yt <= hi)
            out["ci_coverage"] = float(np.mean(inside))
    return out


@dataclass
class RunRecord:
    profile: str
    seed: int
    engine: str
    metrics: dict[str, float]
    n_origins: int


def _walk_forward(engine, series: np.ndarray, holdout_frac: float,
                  ts_index) -> tuple[dict[str, float], int, bool]:
    """Rolling-origin evaluation of one engine on one series.

    At origin t (spanning the holdout tail) the history window is series[:t];
    we forecast, record predicted vs series[t], slide t += 1. Returns the
    metric dict, the number of origins scored, and whether the engine emitted a
    non-degenerate confidence band on any step.
    """
    n = series.size
    start = int(round(n * (1.0 - holdout_frac)))
    start = max(start, 1)  # need at least one prior sample for the window

    preds, truth, lowers, uppers = [], [], [], []
    latencies: list[float] = []
    band_seen = False

    for t in range(start, n):
        hist_rates = series[:t]
        history = HistoryWindow(
            timestamps=[s.isoformat() for s in ts_index[:t]],
            request_rates=hist_rates.tolist(),
        )
        t0 = time.perf_counter()
        fc = engine.forecast(history)
        latencies.append((time.perf_counter() - t0) * 1000.0)

        preds.append(fc.predicted_rps)
        truth.append(float(series[t]))
        lowers.append(fc.confidence_lower)
        uppers.append(fc.confidence_upper)
        if fc.confidence_upper > fc.confidence_lower:
            band_seen = True

    metrics = _metrics(
        np.asarray(preds, dtype="float64"),
        np.asarray(truth, dtype="float64"),
        np.asarray(lowers, dtype="float64"),
        np.asarray(uppers, dtype="float64"),
        has_band=band_seen,
    )
    metrics["latency_ms"] = (
        float(np.mean(latencies)) if latencies else float("nan")
    )
    return metrics, len(preds), band_seen


def _ts_index(n: int):
    """5-minute DatetimeIndex of length n. Only the spacing matters to the
    engines; the absolute epoch is arbitrary."""
    import pandas as pd

    return pd.date_range("2024-01-01", periods=n, freq=BUCKET)


# ── Aggregation + reporting ───────────────────────────────────────────────────
def _aggregate(records: list[RunRecord]) -> "object":
    """Build the per-(engine, profile) and overall mean ± CI tables.

    Uses _bench_common.bench_stats.mean_ci so the CI maths matches the other
    SmartLoad harnesses to the digit. Returns a dict keyed by ("profile"|"ALL",
    engine, metric) → stat dict.
    """
    agg: dict[tuple[str, str, str], dict] = {}

    # Per-profile, in the generator's canonical order (easy → hard) rather than
    # alphabetical, so the tables read steady → diurnal → spiky → ramp.
    present = {r.profile for r in records}
    profiles = [p for p in generators.PROFILES if p in present]
    engines = [e for e in ENGINE_ORDER if any(r.engine == e for r in records)]
    for prof in profiles:
        for eng in engines:
            for m in METRIC_KEYS:
                vals = [
                    r.metrics[m] for r in records
                    if r.profile == prof and r.engine == eng
                ]
                agg[(prof, eng, m)] = bench_stats.mean_ci(vals)

    # Overall roll-up (across all profiles × seeds).
    for eng in engines:
        for m in METRIC_KEYS:
            vals = [r.metrics[m] for r in records if r.engine == eng]
            agg[("ALL", eng, m)] = bench_stats.mean_ci(vals)

    return agg, profiles, engines


def _cell(stat: dict, *, decimals: int, pct: bool = False) -> str:
    """Render a mean ± CI cell; '—' when the metric is entirely NaN (e.g.
    CI-coverage for the band-less naive engine)."""
    if stat["n"] == 0 or (isinstance(stat["mean"], float) and np.isnan(stat["mean"])):
        return "n/a"
    return bench_stats.format_mean_ci(
        stat["mean"], stat["half_width"], stat["n"], decimals=decimals
    )


def _gate_mark(stat: dict) -> str:
    """PASS / FAIL the MAPE<20% gate on the mean. n/a if no MAPE was scored."""
    m = stat["mean"]
    if stat["n"] == 0 or (isinstance(m, float) and np.isnan(m)):
        return "n/a"
    return "PASS" if m < MAPE_GATE else "FAIL"


def _engine_label(eng: str) -> str:
    return {
        "naive": "naive",
        "moving_average": "moving_average",
        "arima_serving": "arima_serving",
        "harmonic_residual": "harmonic_residual",
    }.get(eng, eng)


def _profile_table(agg, profile: str, engines: list[str]) -> list[str]:
    lines = [
        f"### Profile: `{profile}`",
        "",
        "| Engine | MAPE% | sMAPE% | RMSE | MAE | CI-coverage | latency_ms | MAPE<20% |",
        "|---|---:|---:|---:|---:|---:|---:|:--:|",
    ]
    for eng in engines:
        mape = agg[(profile, eng, "mape")]
        lines.append(
            "| `{eng}` | {mape} | {smape} | {rmse} | {mae} | {cov} | {lat} | {gate} |".format(
                eng=_engine_label(eng),
                mape=_cell(mape, decimals=1),
                smape=_cell(agg[(profile, eng, "smape")], decimals=1),
                rmse=_cell(agg[(profile, eng, "rmse")], decimals=2),
                mae=_cell(agg[(profile, eng, "mae")], decimals=2),
                cov=_cell(agg[(profile, eng, "ci_coverage")], decimals=3),
                lat=_cell(agg[(profile, eng, "latency_ms")], decimals=2),
                gate=_gate_mark(mape),
            )
        )
    lines.append("")
    return lines


def _write_summary(out_dir: Path, agg, profiles, engines, *, seeds, days,
                   n_origins_typical: int, statsmodels_version: str) -> None:
    lines = [
        "# Forecasting Engine Benchmark — Results",
        "",
        f"Generated `{out_dir.name}` (UTC). Rolling-origin / walk-forward, "
        f"{HORIZON_STEPS}-step horizon, {BUCKET} buckets.",
        "",
        "Single-step forecasters compared on synthetic RPS series: "
        "**naive** (persistence floor, local — not shipped), **moving_average** "
        f"(window={MA_WINDOW}, shipped), **arima_serving** "
        f"(ARIMA{ARIMA_ORDER} production serving path, shipped), and "
        "**harmonic_residual** (robust dynamic-harmonic-regression + AR(1) "
        "residual with split-conformal bands — the candidate). Every contender "
        "is handed the identical history window at each origin.",
        "",
        f"{len(seeds)} seeds × {len(profiles)} profiles. ~{n_origins_typical} "
        f"scored origins per series (last {HOLDOUT_FRAC:.0%} of a {days}-day span). "
        "All metrics are finite-masked; cells show mean ± 95% CI over seeds.",
        "",
        "> **Out-of-distribution note.** The ARIMA artifact's parameters were fit "
        "on the Alibaba production trace, not on these synthetic series. It is "
        "therefore evaluated out-of-distribution here. That is a fair "
        "generalization test and deliberately avoids the in-sample leakage that "
        "scoring it on a tail of its own training data would introduce.",
        "",
        "## Overall roll-up (all profiles × seeds)",
        "",
        "| Engine | MAPE% | sMAPE% | RMSE | MAE | CI-coverage | latency_ms | MAPE<20% |",
        "|---|---:|---:|---:|---:|---:|---:|:--:|",
    ]
    for eng in engines:
        mape = agg[("ALL", eng, "mape")]
        lines.append(
            "| `{eng}` | {mape} | {smape} | {rmse} | {mae} | {cov} | {lat} | {gate} |".format(
                eng=_engine_label(eng),
                mape=_cell(mape, decimals=1),
                smape=_cell(agg[("ALL", eng, "smape")], decimals=1),
                rmse=_cell(agg[("ALL", eng, "rmse")], decimals=2),
                mae=_cell(agg[("ALL", eng, "mae")], decimals=2),
                cov=_cell(agg[("ALL", eng, "ci_coverage")], decimals=3),
                lat=_cell(agg[("ALL", eng, "latency_ms")], decimals=2),
                gate=_gate_mark(mape),
            )
        )
    lines += ["", "## Per-profile breakdown", ""]
    for prof in profiles:
        lines += _profile_table(agg, prof, engines)

    lines += [
        "## How to read this",
        "",
        "- **MAPE** is the headline (SOT KPI: < 20%). The `MAPE<20%` column marks "
        "PASS/FAIL on the seed-mean. **naive** is the floor — an engine that does "
        "not beat persistence is not earning its keep.",
        "- **sMAPE** and **CI-coverage** are the honesty checks. MAPE punishes "
        "under-prediction and over-prediction asymmetrically and blows up near "
        "small actuals; sMAPE is symmetric and bounded. A CI-coverage far from "
        "0.95 means the 95% band is miscalibrated (too narrow if < 0.95, too wide "
        "if ≫ 0.95). **naive** emits no band, so its coverage is `n/a`.",
        "- **RMSE/MAE** are in raw RPS units — useful for absolute error size, not "
        "comparable across profiles with different load levels.",
        "",
        "---",
        "",
        "### Reproducibility footer",
        "",
        f"- statsmodels: `{statsmodels_version}`",
        f"- ARIMA order: `{ARIMA_ORDER}` (d=0 — no differencing)",
        f"- bucket size: `{BUCKET}`",
        f"- horizon: `{HORIZON_STEPS}-step`",
        f"- moving_average window: `{MA_WINDOW}`",
        f"- seeds: `{list(seeds)}`",
        f"- profiles: `{profiles}`",
        f"- holdout fraction: `{HOLDOUT_FRAC}`",
        f"- MAPE gate: `< {MAPE_GATE}%` (per-row PASS/FAIL above)",
        "",
        "Re-run: `python experiments/forecasting-engine-bench/run.py` "
        "(deterministic — same args reproduce these numbers, latency aside).",
    ]
    (out_dir / "SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")


def _write_grid(out_dir: Path, records: list[RunRecord]) -> None:
    fieldnames = ["profile", "seed", "engine", "n_origins", *METRIC_KEYS]
    with (out_dir / "grid.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            row = {
                "profile": r.profile,
                "seed": r.seed,
                "engine": r.engine,
                "n_origins": r.n_origins,
            }
            for k in METRIC_KEYS:
                v = r.metrics.get(k, float("nan"))
                row[k] = "" if (isinstance(v, float) and np.isnan(v)) else round(v, 6)
            writer.writerow(row)


def _write_meta(out_dir: Path, *, seeds, days, params: GenParams,
                engines: list[str], statsmodels_version: str,
                runtime_s: float) -> None:
    import statsmodels  # noqa: PLC0415

    meta = {
        "tag": out_dir.name,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "protocol": {
            "method": "rolling-origin / walk-forward",
            "horizon_steps": HORIZON_STEPS,
            "bucket": BUCKET,
            "holdout_frac": HOLDOUT_FRAC,
            "mape_gate_pct": MAPE_GATE,
        },
        "versions": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "statsmodels": statsmodels_version,
        },
        "arima_order": list(ARIMA_ORDER),
        "moving_average_window": MA_WINDOW,
        "engines": engines,
        "seeds": list(seeds),
        "days": days,
        "profiles": list(generators.PROFILES.keys()),
        "generator_params": params.as_dict(),
        "runtime_seconds": round(runtime_s, 2),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def main(seeds: tuple[int, ...], days: int, engines_selected: tuple[str, ...],
         tag: str | None) -> None:
    import statsmodels  # noqa: PLC0415

    t_start = time.time()
    params = GenParams(n_buckets=days * generators.BUCKETS_PER_DAY)
    engines = _build_engines(engines_selected)
    ordered = [e for e in ENGINE_ORDER if e in engines]

    ts_index = _ts_index(params.n_buckets)

    records: list[RunRecord] = []
    n_origins_typical = 0
    total = len(seeds) * len(generators.PROFILES) * len(ordered)
    done = 0
    for profile in generators.PROFILES:
        for seed in seeds:
            series = generators.generate(profile, seed, params)
            for eng_name in ordered:
                engine = engines[eng_name]
                metrics, n_origins, _ = _walk_forward(
                    engine, series, HOLDOUT_FRAC, ts_index
                )
                n_origins_typical = n_origins
                records.append(RunRecord(profile, seed, eng_name, metrics, n_origins))
                done += 1
                print(
                    f"[forecasting-bench] {done}/{total} "
                    f"profile={profile} seed={seed} engine={eng_name} "
                    f"MAPE={metrics['mape']:.2f}% "
                    f"lat={metrics['latency_ms']:.2f}ms",
                    flush=True,
                )

    tag = tag or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = _HERE / "results" / tag
    out_dir.mkdir(parents=True, exist_ok=True)

    agg, profiles, present_engines = _aggregate(records)
    _write_grid(out_dir, records)
    runtime_s = time.time() - t_start
    _write_summary(
        out_dir, agg, profiles, present_engines,
        seeds=seeds, days=days, n_origins_typical=n_origins_typical,
        statsmodels_version=statsmodels.__version__,
    )
    _write_meta(
        out_dir, seeds=seeds, days=days, params=params,
        engines=present_engines, statsmodels_version=statsmodels.__version__,
        runtime_s=runtime_s,
    )
    print(
        f"[forecasting-bench] wrote {len(records)} run rows -> {out_dir} "
        f"({runtime_s:.1f}s)"
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark naive vs moving_average vs arima_serving "
                    "forecasters on synthetic RPS series (rolling-origin)."
    )
    parser.add_argument(
        "--seeds", type=int, nargs="+", default=list(DEFAULT_SEEDS),
        help=f"seeds, one series per (profile, seed) (default: {list(DEFAULT_SEEDS)})",
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS,
        help=f"series span in days at {BUCKET} buckets (default: {DEFAULT_DAYS})",
    )
    parser.add_argument(
        "--engines", nargs="+", default=list(ENGINE_ORDER), choices=list(ENGINE_ORDER),
        help=f"engines to run (default: all {list(ENGINE_ORDER)})",
    )
    parser.add_argument(
        "--tag", type=str, default=None,
        help="output sub-directory name (default: UTC timestamp)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    main(tuple(args.seeds), args.days, tuple(args.engines), args.tag)
