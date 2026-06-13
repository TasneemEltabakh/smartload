"""
experiments/baseline-vs-smartload/scripts/plot_results.py
─────────────────────────────────────────────────────────
Plots for the SmartLoad vs NGINX-RR benchmark (#148), multi-run aware (#160).

Reads Locust CSV output from a results directory and overlays baseline vs
smartload. A directory with `run-*` subfolders is treated as a multi-run batch:
each metric is drawn as the per-side **mean line with a 95% CI band** across
runs (Student's t). A directory with `<side>/` folders directly under it is a
single (legacy) run, where the band collapses to a single line.

Inputs (per side, under <root>/[run-NN/]<side>/):
  locust_stats.csv             — aggregate per-name stats (final)
  locust_stats_history.csv     — time-series of per-name stats (--csv-full-history)

Outputs (at the batch/run top level):
  plot_rps.png            — requests/second over time, both sides overlaid
  plot_p50_p95_p99.png    — p50/p95/p99 latency over time
  plot_error_rate.png     — failures/second over time
  plot_total_requests.png — cumulative request count over time
  plot_per_phase_p95.png  — bar chart of p95 per phase per side (CI error bars)
  plot_recovery_curve.png — failures/second around the anomaly window

Each time-series plot shades the anomaly window. Multi-run SUMMARY.md +
summary.parquet are produced by aggregate_runs.py, not here.

Usage:
  python experiments/baseline-vs-smartload/scripts/plot_results.py <results-or-batch-dir>
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

try:
    import pandas as pd
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    print("FAIL: install pandas + matplotlib first (pip install pandas matplotlib)", file=sys.stderr)
    sys.exit(2)

# _bench_common lives at experiments/_bench_common — add experiments/ to path
# for the multi-run confidence-interval maths (#160).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _bench_common import bench_stats  # noqa: E402


SIDES = ("baseline", "smartload")
SIDE_COLORS = {"baseline": "#888888", "smartload": "#1f77b4"}


# ── loaders ────────────────────────────────────────────────────────────────────

def _normalise_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Map Locust column names to snake_case: `Requests/s`→`requests_s`,
    `50%`→`50p`, `Total Failure Count`→`total_failure_count`, etc."""
    rename = {c: c.strip().lower().replace(" ", "_").replace("%", "p").replace("/", "_")
              for c in df.columns}
    return df.rename(columns=rename)


def _load_history(side_dir: Path) -> pd.DataFrame | None:
    path = side_dir / "locust_stats_history.csv"
    if not path.exists():
        return None
    return _normalise_cols(pd.read_csv(path))


def _load_manifest(run_dir: Path) -> dict:
    path = run_dir / "MANIFEST.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _side_dirs(root: Path, side: str) -> list[Path]:
    """Per-side directories: one per run for a batch, else the single side dir."""
    runs = sorted(p for p in root.glob("run-*") if p.is_dir())
    if runs:
        return [r / side for r in runs if (r / side).is_dir()]
    d = root / side
    return [d] if d.is_dir() else []


def _is_batch(root: Path) -> bool:
    return any(p.is_dir() for p in root.glob("run-*"))


# ── multi-run band alignment ───────────────────────────────────────────────────

def _aligned_band(side_dirs: list[Path], value_col: str, max_secs: float):
    """Align each run's Aggregated `value_col` on an integer seconds-since-start
    grid, then return (grid, mean, ci_lower, ci_upper). One run → CI collapses
    to the mean (band == line)."""
    grid = list(range(0, int(max_secs) + 1))
    series: list[pd.Series] = []
    for sd in side_dirs:
        df = _load_history(sd)
        if df is None or df.empty or "name" not in df.columns:
            continue
        agg = df[df["name"] == "Aggregated"].copy()
        if agg.empty or value_col not in agg.columns or "timestamp" not in agg.columns:
            continue
        agg["sec"] = (agg["timestamp"] - agg["timestamp"].min()).round().astype(int)
        col = agg.groupby("sec")[value_col].mean().reindex(grid).interpolate(limit_direction="both")
        series.append(col)
    if not series:
        return grid, None, None, None

    mat = pd.concat(series, axis=1)
    means, los, his = [], [], []
    for _, vals in mat.iterrows():
        st = bench_stats.mean_ci(vals.tolist())
        m = st["mean"]
        means.append(m)
        los.append(m if math.isnan(st["ci_lower"]) else st["ci_lower"])
        his.append(m if math.isnan(st["ci_upper"]) else st["ci_upper"])
    return grid, means, los, his


def _annotate_phases(ax, knobs: dict) -> None:
    at = knobs.get("ANOMALY_AT_SECS", 0)
    hold = knobs.get("ANOMALY_HOLD_SECS", 0)
    if at and hold:
        ax.axvspan(at, at + hold, alpha=0.15, color="red", label="anomaly window")


def _band_timeseries(root: Path, knobs: dict, value_col: str, *, ylabel: str,
                     title: str, out_name: str, n_runs: int) -> None:
    max_secs = knobs.get("SUSTAIN_END_SECS", 360)
    fig, ax = plt.subplots(figsize=(10, 5))
    drew = False
    for side in SIDES:
        grid, mean, lo, hi = _aligned_band(_side_dirs(root, side), value_col, max_secs)
        if mean is None:
            continue
        ax.plot(grid, mean, color=SIDE_COLORS[side], label=side, linewidth=1.6)
        if n_runs > 1:
            ax.fill_between(grid, lo, hi, color=SIDE_COLORS[side], alpha=0.18)
        drew = True
    if not drew:
        ax.text(0.5, 0.5, f"no history data for {value_col}", ha="center", va="center",
                transform=ax.transAxes)
        ax.set_axis_off()
    else:
        _annotate_phases(ax, knobs)
        ci_note = " (mean ± 95% CI)" if n_runs > 1 else ""
        ax.set_xlabel("seconds since shape start")
        ax.set_ylabel(ylabel)
        ax.set_title(title + ci_note)
        ax.legend(loc="best")
        ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(root / out_name, dpi=120)
    plt.close(fig)


def _plot_latencies(root: Path, knobs: dict, n_runs: int) -> None:
    max_secs = knobs.get("SUSTAIN_END_SECS", 360)
    fig, axes = plt.subplots(3, 1, figsize=(10, 9), sharex=True)
    for (col, ax, ylabel) in (("50p", axes[0], "p50 latency (ms)"),
                              ("95p", axes[1], "p95 latency (ms)"),
                              ("99p", axes[2], "p99 latency (ms)")):
        for side in SIDES:
            grid, mean, lo, hi = _aligned_band(_side_dirs(root, side), col, max_secs)
            if mean is None:
                continue
            ax.plot(grid, mean, color=SIDE_COLORS[side], label=side, linewidth=1.5)
            if n_runs > 1:
                ax.fill_between(grid, lo, hi, color=SIDE_COLORS[side], alpha=0.18)
        ax.set_ylabel(ylabel)
        ax.grid(alpha=0.3)
        _annotate_phases(ax, knobs)
        ax.legend(loc="best")
    axes[2].set_xlabel("seconds since shape start")
    fig.suptitle("Latency percentiles: baseline vs smartload" + (" (mean ± 95% CI)" if n_runs > 1 else ""))
    fig.tight_layout()
    fig.savefig(root / "plot_p50_p95_p99.png", dpi=120)
    plt.close(fig)


def _plot_per_phase_p95(root: Path, n_runs: int) -> None:
    """Per-phase p95 bars per side with CI error bars, read from summary.parquet
    when present; falls back to the single-run stats CSV otherwise."""
    fig, ax = plt.subplots(figsize=(10, 5))
    phases = ["A_ramp", "A_hold", "B_anomaly", "C_sustain"]
    width = 0.35
    x = list(range(len(phases)))
    summary_path = root / "summary.parquet"
    summary = pd.read_parquet(summary_path) if summary_path.exists() else None

    for i, side in enumerate(SIDES):
        means, errs = [], []
        for phase in phases:
            mean, err = 0.0, 0.0
            if summary is not None:
                row = summary[(summary["side"] == side) & (summary["phase"] == phase)
                              & (summary["metric"] == "latency_p95_ms")]
                if not row.empty:
                    mean = float(row.iloc[0]["mean"])
                    hw = float(row.iloc[0]["half_width"])
                    err = 0.0 if math.isnan(hw) else hw
            else:
                sds = _side_dirs(root, side)
                if sds:
                    stats = sds[0] / "locust_stats.csv"
                    if stats.exists():
                        df = _normalise_cols(pd.read_csv(stats))
                        r = df[df["name"].astype(str).str.lower() == f"get-/-{phase.lower()}"]
                        if not r.empty and "95p" in df.columns:
                            mean = float(r.iloc[0]["95p"])
            means.append(mean)
            errs.append(err)
        offset = (i - 0.5) * width
        ax.bar([xi + offset for xi in x], means, width=width, color=SIDE_COLORS[side],
               label=side, yerr=errs if any(errs) else None, capsize=3,
               error_kw={"alpha": 0.7})
    ax.set_xticks(x)
    ax.set_xticklabels(phases)
    ax.set_ylabel("p95 latency (ms)")
    ax.set_title("Per-phase p95: baseline vs smartload" + (" (mean ± 95% CI)" if n_runs > 1 else ""))
    ax.legend(loc="best")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(root / "plot_per_phase_p95.png", dpi=120)
    plt.close(fig)


def _plot_recovery(root: Path, knobs: dict, n_runs: int) -> None:
    """Zoom into ±60s around the anomaly window on failures/second."""
    at = knobs.get("ANOMALY_AT_SECS", 0)
    hold = knobs.get("ANOMALY_HOLD_SECS", 0)
    if not at or not hold:
        return
    max_secs = knobs.get("SUSTAIN_END_SECS", 360)
    fig, ax = plt.subplots(figsize=(10, 5))
    lo_x, hi_x = max(0, at - 60), at + hold + 60
    for side in SIDES:
        grid, mean, lo, hi = _aligned_band(_side_dirs(root, side), "failures_s", max_secs)
        if mean is None:
            continue
        gx = [g for g in grid if lo_x <= g <= hi_x]
        idx = [grid.index(g) for g in gx]
        ax.plot(gx, [mean[i] for i in idx], color=SIDE_COLORS[side], label=side, linewidth=1.8)
        if n_runs > 1:
            ax.fill_between(gx, [lo[i] for i in idx], [hi[i] for i in idx],
                            color=SIDE_COLORS[side], alpha=0.18)
    ax.axvspan(at, at + hold, alpha=0.15, color="red", label="anomaly window")
    ax.set_xlabel("seconds since shape start (zoomed)")
    ax.set_ylabel("failures / second")
    ax.set_title("Recovery: failures near the anomaly window" + (" (mean ± 95% CI)" if n_runs > 1 else ""))
    ax.legend(loc="best")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(root / "plot_recovery_curve.png", dpi=120)
    plt.close(fig)


def plot_batch(root: Path) -> None:
    """Render every plot at `root` (a batch dir or a single-run dir)."""
    knobs = _load_manifest(root).get("knobs", {})
    n_runs = max((len(_side_dirs(root, s)) for s in SIDES), default=0)
    label = f"{n_runs}-run batch" if _is_batch(root) else "single run"
    print(f"[plot] {root.name} — {label}")

    _band_timeseries(root, knobs, "requests_s", ylabel="requests / second",
                     title="Sustained RPS: baseline vs smartload",
                     out_name="plot_rps.png", n_runs=n_runs); print("[plot] plot_rps.png")
    _plot_latencies(root, knobs, n_runs); print("[plot] plot_p50_p95_p99.png")
    _band_timeseries(root, knobs, "failures_s", ylabel="failures / second",
                     title="Failure rate: baseline vs smartload",
                     out_name="plot_error_rate.png", n_runs=n_runs); print("[plot] plot_error_rate.png")
    _band_timeseries(root, knobs, "total_request_count", ylabel="cumulative requests",
                     title="Cumulative request count: baseline vs smartload",
                     out_name="plot_total_requests.png", n_runs=n_runs); print("[plot] plot_total_requests.png")
    _plot_per_phase_p95(root, n_runs); print("[plot] plot_per_phase_p95.png")
    _plot_recovery(root, knobs, n_runs); print("[plot] plot_recovery_curve.png")


def main() -> int:
    p = argparse.ArgumentParser(description="Plot SmartLoad vs NGINX-RR benchmark results (#148/#160)")
    p.add_argument("run_dir", type=Path, help="Path to results/<timestamp>/ (batch or single run)")
    args = p.parse_args()
    run_dir: Path = args.run_dir.resolve()
    if not run_dir.is_dir():
        print(f"FAIL: not a directory: {run_dir}", file=sys.stderr)
        return 2
    plot_batch(run_dir)
    print(f"\nWrote plots to {run_dir}")
    if _is_batch(run_dir):
        print("(SUMMARY.md + summary.parquet come from aggregate_runs.py)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
