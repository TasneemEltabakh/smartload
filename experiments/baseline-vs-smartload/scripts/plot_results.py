"""
experiments/baseline-vs-smartload/scripts/plot_results.py
─────────────────────────────────────────────────────────
Reads locust's CSV outputs from a `results/<timestamp>/` directory and
produces six PNG plots comparing baseline (NGINX RR) to smartload
(full decision plane).

Inputs (per side, under <run-dir>/<side>/):
  locust_stats.csv             — aggregate per-name stats (final)
  locust_stats_history.csv     — time-series of per-name stats (--csv-full-history)
  locust_failures.csv          — failure counts
  scaling_audit.json           — autoscaler audit rows captured post-run

Outputs (committed to the same run dir):
  plot_rps.png            — requests-per-second over time, both sides overlaid
  plot_p50_p95_p99.png    — p50/p95/p99 latency over time
  plot_error_rate.png     — error rate (4xx + 5xx) over time
  plot_total_requests.png — cumulative request count over time
  plot_per_phase_p95.png  — bar chart of p95 per phase per side
  plot_recovery_curve.png — error-rate around the anomaly injection window

Each plot is annotated with the anomaly injection window (phase B).

Usage:
  python experiments/baseline-vs-smartload/scripts/plot_results.py \
      experiments/baseline-vs-smartload/results/<timestamp>
"""

from __future__ import annotations

import argparse
import json
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


SIDES = ("baseline", "smartload")
SIDE_COLORS = {"baseline": "#888888", "smartload": "#1f77b4"}


def _load_history(side_dir: Path) -> pd.DataFrame | None:
    path = side_dir / "locust_stats_history.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    # Locust column names contain spaces and percent signs; normalise them
    # so downstream code can use df["p95"] etc.
    rename = {}
    for col in df.columns:
        cl = col.strip().lower().replace(" ", "_").replace("%", "p").replace("/", "_")
        rename[col] = cl
    df = df.rename(columns=rename)
    # Restrict to aggregate rows (Name == "Aggregated") for whole-run series.
    return df


def _load_manifest(run_dir: Path) -> dict:
    path = run_dir / "MANIFEST.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _annotate_phases(ax, knobs: dict) -> None:
    """Shade the anomaly window on every time-series plot."""
    at = knobs.get("ANOMALY_AT_SECS", 0)
    hold = knobs.get("ANOMALY_HOLD_SECS", 0)
    if at and hold:
        ax.axvspan(at, at + hold, alpha=0.15, color="red", label="anomaly window")


def _plot_rps(run_dir: Path, knobs: dict) -> None:
    fig, ax = plt.subplots(figsize=(10, 5))
    for side in SIDES:
        df = _load_history(run_dir / side)
        if df is None or df.empty:
            continue
        agg = df[df["name"] == "Aggregated"].copy()
        if agg.empty:
            continue
        t0 = agg["timestamp"].min()
        agg["t"] = agg["timestamp"] - t0
        if "current_rps" in agg.columns:
            ax.plot(agg["t"], agg["current_rps"], color=SIDE_COLORS[side], label=side, linewidth=1.5)
    _annotate_phases(ax, knobs)
    ax.set_xlabel("seconds since shape start")
    ax.set_ylabel("requests / second")
    ax.set_title("Sustained RPS: baseline vs smartload")
    ax.legend(loc="best")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(run_dir / "plot_rps.png", dpi=120)
    plt.close(fig)


def _plot_latencies(run_dir: Path, knobs: dict) -> None:
    fig, axes = plt.subplots(3, 1, figsize=(10, 9), sharex=True)
    pcols = [
        ("p50_response_time", axes[0], "p50 latency (ms)"),
        ("p95_response_time", axes[1], "p95 latency (ms)"),
        ("p99_response_time", axes[2], "p99 latency (ms)"),
    ]
    for side in SIDES:
        df = _load_history(run_dir / side)
        if df is None or df.empty:
            continue
        agg = df[df["name"] == "Aggregated"].copy()
        if agg.empty:
            continue
        t0 = agg["timestamp"].min()
        agg["t"] = agg["timestamp"] - t0
        for col, ax, ylabel in pcols:
            if col in agg.columns:
                ax.plot(agg["t"], agg[col], color=SIDE_COLORS[side], label=side, linewidth=1.5)
                ax.set_ylabel(ylabel)
                ax.grid(alpha=0.3)
                _annotate_phases(ax, knobs)
                ax.legend(loc="best")
    axes[2].set_xlabel("seconds since shape start")
    fig.suptitle("Latency percentiles: baseline vs smartload")
    fig.tight_layout()
    fig.savefig(run_dir / "plot_p50_p95_p99.png", dpi=120)
    plt.close(fig)


def _plot_error_rate(run_dir: Path, knobs: dict) -> None:
    fig, ax = plt.subplots(figsize=(10, 5))
    for side in SIDES:
        df = _load_history(run_dir / side)
        if df is None or df.empty:
            continue
        agg = df[df["name"] == "Aggregated"].copy()
        if agg.empty:
            continue
        t0 = agg["timestamp"].min()
        agg["t"] = agg["timestamp"] - t0
        # Failure rate per second = total_failure_count delta / interval.
        if "total_failure_count" in agg.columns and "current_rps" in agg.columns:
            agg["fail_delta"] = agg["total_failure_count"].diff().fillna(0)
            # Locust history rows are at 2s intervals by default.
            ax.plot(agg["t"], agg["fail_delta"] / 2.0, color=SIDE_COLORS[side], label=side, linewidth=1.5)
    _annotate_phases(ax, knobs)
    ax.set_xlabel("seconds since shape start")
    ax.set_ylabel("failures / second")
    ax.set_title("Failure rate: baseline vs smartload")
    ax.legend(loc="best")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(run_dir / "plot_error_rate.png", dpi=120)
    plt.close(fig)


def _plot_total_requests(run_dir: Path, knobs: dict) -> None:
    fig, ax = plt.subplots(figsize=(10, 5))
    for side in SIDES:
        df = _load_history(run_dir / side)
        if df is None or df.empty:
            continue
        agg = df[df["name"] == "Aggregated"].copy()
        if agg.empty:
            continue
        t0 = agg["timestamp"].min()
        agg["t"] = agg["timestamp"] - t0
        if "total_request_count" in agg.columns:
            ax.plot(agg["t"], agg["total_request_count"], color=SIDE_COLORS[side], label=side, linewidth=1.5)
    _annotate_phases(ax, knobs)
    ax.set_xlabel("seconds since shape start")
    ax.set_ylabel("cumulative requests")
    ax.set_title("Cumulative request count: baseline vs smartload")
    ax.legend(loc="best")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(run_dir / "plot_total_requests.png", dpi=120)
    plt.close(fig)


def _plot_per_phase_p95(run_dir: Path, knobs: dict) -> None:
    """Bar chart of p95 latency per phase, per side, from the final stats CSV."""
    fig, ax = plt.subplots(figsize=(10, 5))
    phases = ["A_ramp", "A_hold", "B_anomaly", "C_sustain"]
    width = 0.35
    x = list(range(len(phases)))
    for i, side in enumerate(SIDES):
        stats_path = run_dir / side / "locust_stats.csv"
        if not stats_path.exists():
            continue
        df = pd.read_csv(stats_path)
        rename = {c: c.strip().lower().replace(" ", "_").replace("%", "p") for c in df.columns}
        df = df.rename(columns=rename)
        vals = []
        for phase in phases:
            name = f"GET-/-{phase}"
            row = df[df["name"] == name]
            if row.empty or "95p" not in df.columns:
                vals.append(0)
            else:
                vals.append(float(row["95p"].iloc[0]))
        offset = (i - 0.5) * width
        ax.bar([xi + offset for xi in x], vals, width=width, color=SIDE_COLORS[side], label=side)
    ax.set_xticks(x)
    ax.set_xticklabels(phases)
    ax.set_ylabel("p95 latency (ms)")
    ax.set_title("Per-phase p95: baseline vs smartload")
    ax.legend(loc="best")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(run_dir / "plot_per_phase_p95.png", dpi=120)
    plt.close(fig)


def _plot_recovery(run_dir: Path, knobs: dict) -> None:
    """Zoom into ±60s around the anomaly window to show recovery delta."""
    at = knobs.get("ANOMALY_AT_SECS", 0)
    hold = knobs.get("ANOMALY_HOLD_SECS", 0)
    if not at or not hold:
        return
    fig, ax = plt.subplots(figsize=(10, 5))
    for side in SIDES:
        df = _load_history(run_dir / side)
        if df is None or df.empty:
            continue
        agg = df[df["name"] == "Aggregated"].copy()
        if agg.empty:
            continue
        t0 = agg["timestamp"].min()
        agg["t"] = agg["timestamp"] - t0
        window = agg[(agg["t"] >= max(0, at - 60)) & (agg["t"] <= at + hold + 60)]
        if window.empty or "total_failure_count" not in window.columns:
            continue
        window = window.copy()
        window["fail_delta"] = window["total_failure_count"].diff().fillna(0)
        ax.plot(window["t"], window["fail_delta"] / 2.0, color=SIDE_COLORS[side], label=side, linewidth=1.8)
    ax.axvspan(at, at + hold, alpha=0.15, color="red", label="anomaly window")
    ax.set_xlabel("seconds since shape start (zoomed)")
    ax.set_ylabel("failures / second")
    ax.set_title("Recovery: failures near the anomaly window")
    ax.legend(loc="best")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(run_dir / "plot_recovery_curve.png", dpi=120)
    plt.close(fig)


def _summarise(run_dir: Path) -> None:
    """Print one-line summaries per side + an overall delta sentence."""
    lines = ["# Summary", ""]
    side_stats = {}
    for side in SIDES:
        stats_path = run_dir / side / "locust_stats.csv"
        if not stats_path.exists():
            lines.append(f"- {side}: no stats (run skipped or failed)")
            continue
        df = pd.read_csv(stats_path)
        rename = {c: c.strip().lower().replace(" ", "_").replace("%", "p") for c in df.columns}
        df = df.rename(columns=rename)
        agg = df[df["name"] == "Aggregated"]
        if agg.empty:
            agg = df[df["name"].str.lower() == "aggregated"]
        if agg.empty:
            lines.append(f"- {side}: aggregate row missing")
            continue
        row = agg.iloc[0]
        side_stats[side] = {
            "requests": int(row.get("request_count", 0)),
            "failures": int(row.get("failure_count", 0)),
            "p50": float(row.get("50p", row.get("median_response_time", 0))),
            "p95": float(row.get("95p", 0)),
            "p99": float(row.get("99p", 0)),
            "rps": float(row.get("requests_s", 0)),
        }
        lines.append(
            f"- **{side}**: {side_stats[side]['requests']} reqs "
            f"(fails={side_stats[side]['failures']}), "
            f"rps={side_stats[side]['rps']:.1f}, "
            f"p50={side_stats[side]['p50']:.0f}ms · "
            f"p95={side_stats[side]['p95']:.0f}ms · "
            f"p99={side_stats[side]['p99']:.0f}ms"
        )
    if len(side_stats) == 2:
        bl = side_stats["baseline"]
        sl = side_stats["smartload"]
        lines.append("")
        lines.append(
            f"**Delta (smartload vs baseline):** "
            f"p95 {sl['p95'] - bl['p95']:+.0f}ms · "
            f"p99 {sl['p99'] - bl['p99']:+.0f}ms · "
            f"failures {sl['failures'] - bl['failures']:+d}"
        )
    (run_dir / "SUMMARY.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def main() -> int:
    p = argparse.ArgumentParser(description="Plot SmartLoad vs NGINX RR benchmark results")
    p.add_argument("run_dir", type=Path, help="Path to results/<timestamp>/")
    args = p.parse_args()
    run_dir: Path = args.run_dir.resolve()
    if not run_dir.is_dir():
        print(f"FAIL: not a directory: {run_dir}", file=sys.stderr)
        return 2
    manifest = _load_manifest(run_dir)
    knobs = manifest.get("knobs", {})
    _plot_rps(run_dir, knobs)
    _plot_latencies(run_dir, knobs)
    _plot_error_rate(run_dir, knobs)
    _plot_total_requests(run_dir, knobs)
    _plot_per_phase_p95(run_dir, knobs)
    _plot_recovery(run_dir, knobs)
    _summarise(run_dir)
    print(f"\nWrote plots + SUMMARY.md to {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
