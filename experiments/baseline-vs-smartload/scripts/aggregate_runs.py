"""
experiments/baseline-vs-smartload/scripts/aggregate_runs.py
────────────────────────────────────────────────────────────
Multi-run batch aggregator for the baseline-vs-SmartLoad benchmark
(#160, SOT §35.3).

A `RUNS=N bash run_experiment.sh` batch lands per-run folders under
`results/<timestamp>/run-NN/<side>/`. This script reads each run's per-side
Locust final stats, extracts per-phase per-metric values, and aggregates them
across runs into per-metric **mean ± confidence interval** (Student's t),
writing:

  summary.parquet  — tidy long table: side, phase, metric, mean, std, ci_*, n
  SUMMARY.md       — per-side per-phase mean ± CI tables + smartload−baseline
                     delta, at the batch top level

then renders the error-band plots (plot_results.plot_batch).

Usage:
  python experiments/baseline-vs-smartload/scripts/aggregate_runs.py <batch-dir>
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

import plot_results  # noqa: E402  (sibling module in this scripts/ dir)


SIDES: tuple[str, ...] = ("baseline", "smartload")

# Phase request-name suffix (locustfile tags requests `GET-/-<phase>`).
PHASES: tuple[str, ...] = ("A_ramp", "A_hold", "B_anomaly", "C_sustain")

METRICS: dict[str, tuple[str, str, int]] = {
    "latency_p50_ms": ("p50 latency", "ms", 0),
    "latency_p95_ms": ("p95 latency", "ms", 0),
    "latency_p99_ms": ("p99 latency", "ms", 0),
    "error_rate_pct": ("error rate", "%", 2),
    "rps":            ("throughput", "rps", 1),
}


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Locust column names carry spaces, percent signs and slashes; map them to
    snake_case so `50%`→`50p`, `Requests/s`→`requests_s`, etc."""
    rename = {c: c.strip().lower().replace(" ", "_").replace("%", "p").replace("/", "_")
              for c in df.columns}
    return df.rename(columns=rename)


def _phase_seconds(knobs: dict) -> dict[str, float]:
    ramp = float(knobs.get("RAMP_SECS", 60))
    at = float(knobs.get("ANOMALY_AT_SECS", 120))
    hold = float(knobs.get("ANOMALY_HOLD_SECS", 60))
    end = float(knobs.get("SUSTAIN_END_SECS", 360))
    return {
        "A_ramp":    max(1.0, ramp),
        "A_hold":    max(1.0, at - ramp),
        "B_anomaly": max(1.0, hold),
        "C_sustain": max(1.0, end - (at + hold)),
        "overall":   max(1.0, end),
    }


def per_run_side_metrics(stats_csv: Path, knobs: dict) -> list[dict]:
    """Extract per-phase per-metric values from one run/side `locust_stats.csv`.
    Returns {phase, metric, value} rows."""
    if not stats_csv.exists():
        return []
    df = _normalise(pd.read_csv(stats_csv))
    if "name" not in df.columns:
        return []
    phase_secs = _phase_seconds(knobs)

    rows: list[dict] = []
    # Per-phase rows from the named requests, plus an "overall" from Aggregated.
    targets = {p: f"get-/-{p.lower()}" for p in PHASES}
    targets["overall"] = "aggregated"
    for phase, name in targets.items():
        row = df[df["name"].astype(str).str.lower() == name]
        if row.empty:
            continue
        r = row.iloc[0]

        def _f(col: str):
            return float(r[col]) if col in df.columns and pd.notna(r[col]) else None

        req = _f("request_count") or 0.0
        fail = _f("failure_count") or 0.0
        vals = {
            "latency_p50_ms": _f("50p") if "50p" in df.columns else _f("median_response_time"),
            "latency_p95_ms": _f("95p"),
            "latency_p99_ms": _f("99p"),
            "error_rate_pct": (100.0 * fail / req) if req > 0 else 0.0,
            "rps":            (req / phase_secs.get(phase, 1.0)) if req > 0 else 0.0,
        }
        for metric, value in vals.items():
            if value is not None:
                rows.append({"phase": phase, "metric": metric, "value": value})
    return rows


def discover_runs(batch_dir: Path) -> list[Path]:
    runs = sorted(p for p in batch_dir.glob("run-*") if p.is_dir())
    if runs:
        return runs
    # Legacy single-run layout: results/<ts>/<side>/ directly under batch_dir.
    if any((batch_dir / s).is_dir() for s in SIDES):
        return [batch_dir]
    return []


def _load_knobs(batch_dir: Path) -> dict:
    mpath = batch_dir / "MANIFEST.json"
    if mpath.exists():
        try:
            return json.loads(mpath.read_text()).get("knobs", {})
        except json.JSONDecodeError:
            return {}
    return {}


def aggregate(batch_dir: Path) -> pd.DataFrame:
    knobs = _load_knobs(batch_dir)
    run_dirs = discover_runs(batch_dir)

    long_rows: list[dict] = []
    for k, rd in enumerate(run_dirs, start=1):
        for side in SIDES:
            stats_csv = rd / side / "locust_stats.csv"
            for row in per_run_side_metrics(stats_csv, knobs):
                long_rows.append({"run_index": k, "side": side, **row})

    long_df = pd.DataFrame(long_rows)
    summary = bench_stats.summarize_runs(long_df, group_keys=["side", "phase", "metric"])

    if not summary.empty:
        order_phase = {p: i for i, p in enumerate([*PHASES, "overall"])}
        order_metric = {m: i for i, m in enumerate(METRICS)}
        summary["_s"] = summary["side"].map({s: i for i, s in enumerate(SIDES)}).fillna(99)
        summary["_p"] = summary["phase"].map(order_phase).fillna(99)
        summary["_m"] = summary["metric"].map(order_metric).fillna(99)
        summary = summary.sort_values(["_s", "_p", "_m"]).drop(columns=["_s", "_p", "_m"]).reset_index(drop=True)
        summary.to_parquet(batch_dir / "summary.parquet", index=False)

    _write_summary_md(batch_dir, summary, run_dirs, knobs)
    return summary


def _cell(summary: pd.DataFrame, side: str, phase: str, metric: str) -> str:
    row = summary[(summary["side"] == side) & (summary["phase"] == phase) & (summary["metric"] == metric)]
    if row.empty:
        return "—"
    r = row.iloc[0]
    _, unit, decimals = METRICS[metric]
    return bench_stats.format_mean_ci(float(r["mean"]), float(r["half_width"]),
                                      int(r["n"]), decimals=decimals, unit=unit)


def _write_summary_md(batch_dir: Path, summary: pd.DataFrame,
                      run_dirs: list[Path], knobs: dict) -> None:
    manifest = {}
    mpath = batch_dir / "MANIFEST.json"
    if mpath.exists():
        try:
            manifest = json.loads(mpath.read_text())
        except json.JSONDecodeError:
            manifest = {}
    n_runs = knobs.get("RUNS", len(run_dirs))
    seed_base = knobs.get("SEED_BASE", "?")
    git_sha = (manifest.get("git_sha") or "?")[:8]
    git_state = manifest.get("git_state", "?")
    short = knobs.get("SHORT", "?")
    metric_labels = [lbl for (lbl, _u, _d) in METRICS.values()]
    phase_list = [*PHASES, "overall"]

    sections: list[str] = []
    for side in SIDES:
        header = "| Phase | " + " | ".join(metric_labels) + " |"
        divider = "|" + "---|" * (len(METRICS) + 1)
        rows = [f"### {side}", "", header, divider]
        for phase in phase_list:
            cells = [_cell(summary, side, phase, m) for m in METRICS]
            rows.append(f"| `{phase}` | " + " | ".join(cells) + " |")
        sections.append("\n".join(rows))

    # Delta (smartload − baseline) on the headline metrics, per phase.
    delta_rows = ["| Phase | Δ p95 latency | Δ error rate | Δ throughput |",
                  "|---|---|---|---|"]

    def _mean(side, phase, metric):
        row = summary[(summary["side"] == side) & (summary["phase"] == phase) & (summary["metric"] == metric)]
        return float(row.iloc[0]["mean"]) if not row.empty else None

    for phase in phase_list:
        parts = []
        for metric, unit, dec in (("latency_p95_ms", "ms", 0), ("error_rate_pct", "pp", 2), ("rps", "rps", 1)):
            sl = _mean("smartload", phase, metric)
            bl = _mean("baseline", phase, metric)
            parts.append(f"{sl - bl:+.{dec}f} {unit}" if (sl is not None and bl is not None) else "—")
        delta_rows.append(f"| `{phase}` | " + " | ".join(parts) + " |")

    n_observed = sorted(int(n) for n in summary["n"].unique()) if not summary.empty else []
    n_note = (f"n={n_observed[0]}" if len(n_observed) == 1
              else f"n∈{{{','.join(map(str, n_observed))}}}" if n_observed else "n=0")

    body = f"""# Baseline-vs-SmartLoad multi-run SUMMARY — {batch_dir.name}

> **{n_runs} run(s)** per side ({n_note}) · seed_base `{seed_base}` · short={short} · git `{git_sha}` ({git_state})

Per-side per-phase **mean ± 95% confidence interval** (Student's t, df=N−1)
across the batch. Single-run cells show `(n=1)` — no interval is defined for one
sample. The seed fixes Locust's load-gen jitter only; the residual spread the CI
captures is run-to-run variance from cold caches, JIT warm-up and container
start ordering (SOT §35.3).

## Per-side mean ± CI

{(chr(10) + chr(10)).join(sections)}

## Delta (smartload − baseline)

A negative Δ p95 / Δ error rate means SmartLoad is better. Treat a delta whose
magnitude is within the two sides' CI half-widths as **not yet significant** at
this run count.

{chr(10).join(delta_rows)}

## How to read this

- **error rate** is per phase: `100 · failures / requests`. Δ error rate is in
  percentage points (pp).
- **throughput** is per-phase `request_count / phase_seconds`.
- `summary.parquet` carries this in tidy/long form
  (`side, phase, metric, mean, std, ci_lower, ci_upper, half_width, n`).

## Per-run directories

{chr(10).join(f"- `{rd.name}/`" for rd in run_dirs)}
"""
    (batch_dir / "SUMMARY.md").write_text(body, encoding="utf-8")


def analyze_batch(batch_dir: Path) -> None:
    summary = aggregate(batch_dir)
    print(f"[aggregate] summary.parquet: {len(summary)} (side,phase,metric) rows -> {batch_dir / 'summary.parquet'}")
    print(f"[aggregate] SUMMARY.md -> {batch_dir / 'SUMMARY.md'}")
    try:
        plot_results.plot_batch(batch_dir)
    except Exception as exc:  # noqa: BLE001 — plots are best-effort
        print(f"[aggregate] WARN — plotting failed: {exc!r}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="baseline-vs-smartload multi-run aggregator (#160)")
    parser.add_argument("batch_dir", help="Timestamped batch directory produced by run_experiment.sh.")
    args = parser.parse_args()

    batch_dir = Path(args.batch_dir).resolve()
    if not batch_dir.is_dir():
        print(f"ERROR: {batch_dir} is not a directory", file=sys.stderr)
        return 1
    if not discover_runs(batch_dir):
        print(f"ERROR: no run-NN/<side>/ folders under {batch_dir}", file=sys.stderr)
        return 1

    analyze_batch(batch_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
