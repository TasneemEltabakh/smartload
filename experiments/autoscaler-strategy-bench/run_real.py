"""
experiments/autoscaler-strategy-bench/run_real.py
──────────────────────────────────────────────────
Autoscaler strategy benchmark on REAL-WORLD demand traces.

Same controlled comparison and same strategies as run.py, but the demand
realizations come from the shared real-trace corpus (see realtrace.py) instead
of the synthetic shapes:

  azure     Azure Functions 2019  (PRIMARY, smooth diurnal serverless demand)
  worldcup  FIFA World Cup 1998    (real flash crowds — the spike analogue)
  alibaba   Alibaba Cluster 2018   (bursty per-minute demand PROXY, labelled)

Each (source, seed) is one fixed real window, replayed identically through every
strategy. Outputs under results/<tag>/: grid_real.csv, SUMMARY_REAL.md, meta_real.json.

Run:  python experiments/autoscaler-strategy-bench/run_real.py
      python experiments/autoscaler-strategy-bench/run_real.py --seeds 8 --tag real
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
sys.path.insert(0, str(_REPO / "experiments" / "_bench_common"))
sys.path.insert(0, str(_HERE))

from bench_stats import mean_ci, format_mean_ci  # noqa: E402
from realtrace import realtrace_curve, REAL_SOURCES  # noqa: E402
from sim import SimParams, run_strategy, STRATEGIES, STRATEGY_LABELS  # noqa: E402
from run import METRICS, _agg, _table_block, PEAK_MULT, _git_hash  # noqa: E402

# Provenance for the SUMMARY caption (source → human label + license short-form).
SOURCE_LABELS = {
    "azure": "Azure Functions 2019 (PRIMARY, CC-BY)",
    "worldcup": "FIFA World Cup 1998 (flash crowds, CC-BY-4.0)",
    "alibaba": "Alibaba Cluster 2018 (PROXY: instances/min, academic terms)",
}


def run_grid_real(params: SimParams, seeds: list[int]) -> pd.DataFrame:
    peak_rps = PEAK_MULT * params.per_instance_capacity_rps
    rows: list[dict] = []
    for source in REAL_SOURCES:
        for seed in seeds:
            demand = realtrace_curve(source, params.run_steps, peak_rps, seed)
            for strat in STRATEGIES:
                res = run_strategy(strat, demand, params,
                                   cooldown=params.cooldown_seconds, seed=seed)
                for key, *_ in METRICS:
                    rows.append({
                        "profile": source,
                        "strategy": strat,
                        "seed": seed,
                        "metric": key,
                        "value": float(getattr(res, key)),
                    })
    return pd.DataFrame(rows)


def build_summary_real(long_df: pd.DataFrame, params: SimParams,
                       seeds: list[int]) -> str:
    by_ps = _agg(long_df, ["profile", "strategy"])
    by_s = _agg(long_df, ["strategy"])

    caption = (
        f"_Real-trace demand, normalized so each window's peak = "
        f"{PEAK_MULT:.0f}×capacity = {PEAK_MULT * params.per_instance_capacity_rps:.0f} rps. "
        f"Window = 30 min upsampled minute→second; only the shape is real, the "
        f"scale is normalized so every profile grades the same pool. "
        f"per-instance cap = {params.per_instance_capacity_rps:.0f} rps, "
        f"warm-up w = {params.warmup_steps} s, cooldown = {params.cooldown_seconds:.0f} s, "
        f"seeds = {seeds} (n={len(seeds)}; each seed = a different real window). "
        f"Cells: mean ± 95% t-CI._"
    )
    out = [
        "# Autoscaler strategy benchmark — REAL traces",
        "",
        "The same strategies and controlled comparison as the synthetic "
        "benchmark (SUMMARY.md), replayed on real per-minute request traces. "
        "Sources:",
        "",
    ]
    for s in REAL_SOURCES:
        out.append(f"- **{s}** — {SOURCE_LABELS[s]}")
    out += ["", caption, ""]

    for source in REAL_SOURCES:
        rows_for = {strat: by_ps.get((source, strat), {}) for strat in STRATEGIES}
        out.append(_table_block(
            f"Source: {source}",
            f"_Real source **{source}** ({SOURCE_LABELS[source]}), n={len(seeds)} windows._",
            rows_for,
        ))

    rows_for = {strat: by_s.get((strat,), {}) for strat in STRATEGIES}
    out.append(_table_block(
        "Aggregate (all real sources)",
        f"_Aggregate over all {len(REAL_SOURCES)} real sources × {len(seeds)} windows._",
        rows_for,
    ))
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Autoscaler benchmark on real traces")
    ap.add_argument("--seeds", type=int, default=8)
    ap.add_argument("--seed0", type=int, default=1000)
    ap.add_argument("--warmup", type=int, default=20)
    ap.add_argument("--cooldown", type=float, default=60.0)
    ap.add_argument("--tag", type=str, default=None)
    args = ap.parse_args(argv)

    seeds = list(range(args.seed0, args.seed0 + args.seeds))
    params = SimParams(warmup_steps=args.warmup, cooldown_seconds=args.cooldown)
    tag = args.tag or datetime.now(timezone.utc).strftime("real_%Y%m%d_%H%M%S")
    out_dir = _HERE / "results" / tag
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    long_df = run_grid_real(params, seeds)
    runtime_s = time.time() - t0

    long_df.to_csv(out_dir / "grid_real.csv", index=False)
    (out_dir / "SUMMARY_REAL.md").write_text(
        build_summary_real(long_df, params, seeds), encoding="utf-8")

    meta = {
        "tag": tag,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_hash(),
        "runtime_seconds": round(runtime_s, 3),
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "seeds": seeds,
        "n_seeds": len(seeds),
        "real_sources": list(REAL_SOURCES),
        "strategies": list(STRATEGIES),
        "data_root": "/data/smartload-datasets",
        "window_minutes": 30,
        "params": {
            "per_instance_capacity_rps": params.per_instance_capacity_rps,
            "cooldown_seconds": params.cooldown_seconds,
            "warmup_steps": params.warmup_steps,
            "run_steps": params.run_steps,
            "peak_demand_mult_of_capacity": PEAK_MULT,
        },
        "decision_rule": "services/autoscaler/decisions.py::decide",
        "controller": "services/autoscaler/controllers.py::decide_target",
    }
    (out_dir / "meta_real.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(f"[autoscaler-strategy-bench:real] wrote {out_dir}")
    print(f"  grid_real.csv ({len(long_df)} rows)  SUMMARY_REAL.md  meta_real.json")
    print(f"  runtime: {runtime_s:.2f}s  seeds={seeds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
