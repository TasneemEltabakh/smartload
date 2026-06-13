"""
experiments/adaptive-bench/scripts/aggregate_runs.py
─────────────────────────────────────────────────────
Multi-run batch aggregator (#160, SOT §35.3).

A `--runs N` batch lands N per-run folders (run-01 … run-NN) under one
timestamped batch directory. This script:

  1. joins each run (run.parquet etc.) if not already joined,
  2. extracts per-phase per-metric values from each run's run.parquet,
  3. aggregates them across runs into per-metric mean ± confidence interval
     (Student's t, via _bench_common.bench_stats), writing:
        summary.parquet  — tidy long table: phase, metric, mean, std, ci_*, n
        SUMMARY.md       — per-phase mean ± CI table at the batch top level,
  4. renders the error-band plots (plot_results.plot_batch).

Usage:
  python experiments/adaptive-bench/scripts/aggregate_runs.py <batch-dir>

`<batch-dir>` is the timestamped folder produced by run.py. If it has no
`run-*` subfolders the directory itself is treated as a single run (back-compat
with the pre-#160 single-run layout).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

# _bench_common lives at experiments/_bench_common — add experiments/ to path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _bench_common import bench_stats  # noqa: E402

# join_run / plot_results are siblings in this scripts package. Support both
# `python scripts/aggregate_runs.py` (scripts/ on path) and
# `from scripts.aggregate_runs import …` (run.py importing the package).
try:  # pragma: no cover - import-path shim
    from scripts import join_run, plot_results
except ImportError:  # pragma: no cover
    import join_run          # type: ignore[no-redef]
    import plot_results      # type: ignore[no-redef]


# Phase order matches the 5-phase Locust shape (run.py FULL_PHASES / SHORT_PHASES).
PHASES: tuple[str, ...] = (
    "A_bootstrap", "B_forecast_burst", "C_sustain",
    "D_anomaly_scale_down", "E_steady",
)

# Metric → (label, unit, decimals) for the SUMMARY.md table.
METRICS: dict[str, tuple[str, str, int]] = {
    "latency_p50_ms": ("p50 latency", "ms", 0),
    "latency_p95_ms": ("p95 latency", "ms", 0),
    "latency_p99_ms": ("p99 latency", "ms", 0),
    "error_rate_pct": ("error rate", "%", 2),
    "rps":            ("throughput", "rps", 1),
    "replica_count":  ("replica count", "", 1),
}


def discover_runs(batch_dir: Path) -> list[Path]:
    """Return the per-run directories in order. Falls back to [batch_dir] when
    there are no run-* subfolders (single-run / legacy layout)."""
    runs = sorted(p for p in batch_dir.glob("run-*") if p.is_dir())
    return runs or [batch_dir]


def per_run_metrics(run_dir: Path) -> list[dict]:
    """Extract per-phase per-metric values from one run's run.parquet.

    Returns a list of {phase, metric, value} rows. Phases with no data are
    skipped (they contribute no observation to that run, so the CI's n shrinks
    honestly rather than being padded with zeros)."""
    parquet = run_dir / "run.parquet"
    if not parquet.exists():
        return []
    run = pd.read_parquet(parquet)
    if run.empty or "phase" not in run.columns:
        return []

    rows: list[dict] = []
    for phase in PHASES:
        sub = run[run["phase"] == phase]
        if sub.empty:
            continue

        def _median(col: str):
            return float(sub[col].median()) if col in sub.columns and sub[col].notna().any() else None

        vals = {
            "latency_p50_ms": _median("latency_p50_ms"),
            "latency_p95_ms": _median("latency_p95_ms"),
            "latency_p99_ms": _median("latency_p99_ms"),
            "rps":            (float(sub["rps"].mean()) if "rps" in sub.columns and sub["rps"].notna().any() else None),
        }

        # Error rate over the phase from the cumulative counters (delta-based:
        # robust to where the phase window starts).
        if {"total_requests", "total_failures"}.issubset(sub.columns):
            req = float(sub["total_requests"].iloc[-1] - sub["total_requests"].iloc[0])
            fail = float(sub["total_failures"].iloc[-1] - sub["total_failures"].iloc[0])
            vals["error_rate_pct"] = (100.0 * fail / req) if req > 0 else 0.0

        # Peak replica count the phase reached (captures forecast-driven growth).
        if "pool_size_active" in sub.columns and sub["pool_size_active"].notna().any():
            vals["replica_count"] = float(sub["pool_size_active"].max())

        for metric, value in vals.items():
            if value is not None:
                rows.append({"phase": phase, "metric": metric, "value": value})
    return rows


def aggregate(batch_dir: Path) -> pd.DataFrame:
    """Build the per-metric mean ± CI summary across all runs in the batch and
    write summary.parquet + SUMMARY.md. Returns the summary DataFrame."""
    run_dirs = discover_runs(batch_dir)
    long_rows: list[dict] = []
    for k, rd in enumerate(run_dirs, start=1):
        for row in per_run_metrics(rd):
            long_rows.append({"run_index": k, **row})

    long_df = pd.DataFrame(long_rows)
    summary = bench_stats.summarize_runs(long_df, group_keys=["phase", "metric"])

    # Stable ordering: phase then metric in declared order.
    if not summary.empty:
        summary["_p"] = summary["phase"].map({p: i for i, p in enumerate(PHASES)}).fillna(99)
        summary["_m"] = summary["metric"].map({m: i for i, m in enumerate(METRICS)}).fillna(99)
        summary = summary.sort_values(["_p", "_m"]).drop(columns=["_p", "_m"]).reset_index(drop=True)
        summary.to_parquet(batch_dir / "summary.parquet", index=False)

    _write_summary_md(batch_dir, summary, run_dirs)
    return summary


def _read_batch_manifest(run_dirs: list[Path]) -> dict:
    """Read the first run's MANIFEST for batch-level metadata (best-effort)."""
    for rd in run_dirs:
        mpath = rd / "MANIFEST.json"
        if mpath.exists():
            try:
                return json.loads(mpath.read_text())
            except json.JSONDecodeError:
                continue
    return {}


def _write_summary_md(batch_dir: Path, summary: pd.DataFrame, run_dirs: list[Path]) -> None:
    manifest = _read_batch_manifest(run_dirs)
    n_runs = manifest.get("runs_total", len(run_dirs))
    seed_base = manifest.get("seed_base", "?")
    short = manifest.get("short", "?")
    git_sha = (manifest.get("git_sha") or "?")[:8]
    git_state = manifest.get("git_state", "?")
    bench_version = manifest.get("bench_version", "?")
    batch_ts = manifest.get("batch_timestamp") or batch_dir.name

    # Build a phase × metric table of mean ± CI cells.
    def cell(phase: str, metric: str) -> str:
        if summary.empty:
            return "—"
        row = summary[(summary["phase"] == phase) & (summary["metric"] == metric)]
        if row.empty:
            return "—"
        r = row.iloc[0]
        _, unit, decimals = METRICS[metric]
        return bench_stats.format_mean_ci(
            float(r["mean"]), float(r["half_width"]), int(r["n"]),
            decimals=decimals, unit=unit,
        )

    metric_labels = [lbl for (lbl, _u, _d) in METRICS.values()]
    header = "| Phase | " + " | ".join(metric_labels) + " |"
    divider = "|" + "---|" * (len(METRICS) + 1)
    table_rows = [header, divider]
    for phase in PHASES:
        cells = [cell(phase, m) for m in METRICS]
        table_rows.append(f"| `{phase}` | " + " | ".join(cells) + " |")

    n_observed = sorted(int(n) for n in summary["n"].unique()) if not summary.empty else []
    n_note = (f"n={n_observed[0]}" if len(n_observed) == 1
              else f"n∈{{{','.join(map(str, n_observed))}}}" if n_observed else "n=0")

    body = f"""# Adaptive-bench multi-run SUMMARY — {batch_ts}

> Bench `{bench_version}` · **{n_runs} run(s)** ({n_note}) · seed_base `{seed_base}` · short={short} · git `{git_sha}` ({git_state})

Per-phase per-metric **mean ± 95% confidence interval** (Student's t, df=N−1)
across the batch. Single-run cells show `(n=1)` — no interval is defined for one
sample. The seed fixes Locust's load-gen jitter only; the residual spread the CI
captures is run-to-run variance from cold caches, JIT warm-up and container
start ordering (SOT §35.3).

## Per-phase mean ± CI

{chr(10).join(table_rows)}

## How to read this

- **error rate** is delta-based per phase: `100 · Δfailures / Δrequests`.
- **replica count** is the peak active-backend pool the phase reached (the
  forecast-driven scale-out signal for Phase B).
- The full per-run timelines, plots and raw artefacts live under each
  `run-NN/` folder; `summary.parquet` carries this table in tidy/long form
  (`phase, metric, mean, std, ci_lower, ci_upper, half_width, n`).

## Per-run directories

{chr(10).join(f"- `{rd.name}/`" for rd in run_dirs)}
"""
    (batch_dir / "SUMMARY.md").write_text(body, encoding="utf-8")


def analyze_batch(batch_dir: Path) -> None:
    """Join every run, aggregate to summary.parquet + SUMMARY.md, render plots."""
    run_dirs = discover_runs(batch_dir)
    for rd in run_dirs:
        if not (rd / "run.parquet").exists():
            try:
                join_run.join_run_dir(rd)
            except Exception as exc:  # noqa: BLE001
                print(f"[aggregate] WARN — join failed for {rd.name}: {exc!r}", flush=True)

    summary = aggregate(batch_dir)
    print(f"[aggregate] summary.parquet: {len(summary)} (phase,metric) rows -> {batch_dir / 'summary.parquet'}")
    print(f"[aggregate] SUMMARY.md -> {batch_dir / 'SUMMARY.md'}")

    try:
        plot_results.plot_batch(batch_dir)
    except Exception as exc:  # noqa: BLE001 — plots are best-effort
        print(f"[aggregate] WARN — plotting failed: {exc!r}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="adaptive-bench multi-run aggregator (#160)")
    parser.add_argument("batch_dir", help="Timestamped batch directory produced by run.py.")
    args = parser.parse_args()

    batch_dir = Path(args.batch_dir).resolve()
    if not batch_dir.is_dir():
        print(f"ERROR: {batch_dir} is not a directory", file=sys.stderr)
        return 1

    analyze_batch(batch_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
