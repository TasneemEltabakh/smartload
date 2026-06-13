"""
experiments/adaptive-bench/scripts/plot_results.py
───────────────────────────────────────────────────
Round 3 plotting + SUMMARY.md (#157). Reads the joined parquet files
produced by `join_run.py` and emits:

  plot_pool_size.png         — pool size + scaling events
  plot_time_to_react.png     — per-forecast time-to-react bar
  plot_upstream_timeline.png — per-phase p95 with upstream-rewrite markers
  plot_anomaly_recovery.png  — phase D timeline with anomaly + scaling overlay
  SUMMARY.md                 — phase timings, pool stats, action counts

Matplotlib is configured for the `Agg` backend BEFORE pyplot is touched, so
this script is safe to run headless (CI, server, container) where there's
no display.

Usage:
  python experiments/adaptive-bench/scripts/plot_results.py <results-dir>
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")          # MUST come before pyplot import

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt    # noqa: E402
import pandas as pd                # noqa: E402

# _bench_common lives at experiments/_bench_common — add experiments/ to path
# for the multi-run confidence-interval maths (#160).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _bench_common import bench_stats  # noqa: E402


# ── loaders ───────────────────────────────────────────────────────────────────

def _load_or_none(path: Path) -> pd.DataFrame | None:
    return pd.read_parquet(path) if path.exists() else None


def _load_run(run_dir: Path) -> dict:
    """Load every parquet + the manifest. Missing parquets are silently
    treated as empty — keeps the plots robust against runs where a stream
    happened to be empty (e.g. zero scaling events on a quiet bench)."""
    return {
        "manifest": json.loads((run_dir / "MANIFEST.json").read_text()),
        "run":      _load_or_none(run_dir / "run.parquet"),
        "forecasts":      _load_or_none(run_dir / "forecasts.parquet"),
        "anomalies":      _load_or_none(run_dir / "anomalies.parquet"),
        "scalings":       _load_or_none(run_dir / "scalings.parquet"),
        "routings":       _load_or_none(run_dir / "routings.parquet"),
        "scaling_audit":  _load_or_none(run_dir / "scaling_audit.parquet"),
        "upstream_changes": _load_or_none(run_dir / "upstream_changes.parquet"),
    }


# ── plot 1: pool size + scaling markers ───────────────────────────────────────

def plot_pool_size(data: dict, out_path: Path) -> None:
    run    = data["run"]
    audit  = data["scaling_audit"]
    fig, ax = plt.subplots(figsize=(11, 4.5))

    ax.step(run["ts"], run["pool_size_active"], where="post",
            color="#1f77b4", linewidth=2, label="active backends in upstream.conf")
    ax.set_ylabel("pool size (active backends)")
    ax.set_ylim(0, max(6, run["pool_size_active"].max() + 1))

    # Scaling-decision markers (canonical record from the autoscaler audit).
    if audit is not None and not audit.empty:
        for _, row in audit.iterrows():
            colour = "#2ca02c" if row["action"] == "scale_out" else "#d62728"
            ax.axvline(row["ts"], color=colour, linestyle="--", alpha=0.6, linewidth=1)
            ax.annotate(
                f"{row['action']}\nic={int(row['instance_count'])}",
                xy=(row["ts"], 0.05), xycoords=("data", "axes fraction"),
                ha="center", va="bottom", fontsize=8, color=colour,
            )

    _annotate_phases(ax, data, y_text=0.92)

    ax.set_title("Adaptive-bench pool size + autoscaler decisions")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S", tz=run["ts"].dt.tz))
    ax.legend(loc="upper right")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


# ── plot 2: per-forecast time-to-react ────────────────────────────────────────

def plot_time_to_react(data: dict, out_path: Path) -> None:
    """For each forecast publish in the bench window, compute the wall-clock
    delay until the NEXT autoscaler decision (which is the closest analog to
    'first new backend healthy' when no provision() actually fires).
    """
    forecasts = data["forecasts"]
    audit     = data["scaling_audit"]
    fig, ax   = plt.subplots(figsize=(11, 4.5))

    if forecasts is None or forecasts.empty or audit is None or audit.empty:
        ax.text(0.5, 0.5, "no forecast/scaling pairs in this run",
                ha="center", va="center", transform=ax.transAxes, fontsize=11)
        ax.set_axis_off()
        fig.savefig(out_path, dpi=120)
        plt.close(fig)
        return

    rows = []
    for _, fc in forecasts.iterrows():
        ft = fc["captured_at"]
        after = audit[audit["ts"] >= ft]
        if after.empty:
            continue
        first_action = after.iloc[0]
        rows.append({
            "forecast_ts":         ft,
            "forecast_predicted":  fc.get("predicted_rps"),
            "action_ts":           first_action["ts"],
            "action":              first_action["action"],
            "delay_seconds":       (first_action["ts"] - ft).total_seconds(),
        })

    if not rows:
        ax.text(0.5, 0.5, "every forecast preceded its first scaling action by ∞ — "
                "no action fired after a forecast in this run",
                ha="center", va="center", transform=ax.transAxes, fontsize=10)
        ax.set_axis_off()
        fig.savefig(out_path, dpi=120)
        plt.close(fig)
        return

    df = pd.DataFrame(rows)
    bars = ax.bar(range(len(df)), df["delay_seconds"],
                  color=["#2ca02c" if a == "scale_out" else "#d62728" for a in df["action"]])
    for i, b in enumerate(bars):
        ax.annotate(f"{df['delay_seconds'].iloc[i]:.1f}s",
                    xy=(b.get_x() + b.get_width()/2, b.get_height()),
                    xytext=(0, 3), textcoords="offset points",
                    ha="center", va="bottom", fontsize=8)
    ax.set_ylabel("seconds to first autoscaler action after forecast")
    ax.set_xticks(range(len(df)))
    ax.set_xticklabels([f"f{i}\n{p:.0f}rps" for i, p in enumerate(df["forecast_predicted"])],
                       fontsize=8)
    ax.set_title("Time-to-react: forecast publish → next autoscaler action")
    ax.axhline(60, color="gray", linestyle=":", alpha=0.5, linewidth=1)
    ax.annotate("forecast cycle (60s)", xy=(len(df)-0.5, 60),
                xytext=(0, 3), textcoords="offset points",
                ha="right", va="bottom", color="gray", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


# ── plot 3: per-phase p95 with upstream-rewrite markers ──────────────────────

def plot_upstream_timeline(data: dict, out_path: Path) -> None:
    run = data["run"]
    ups = data["upstream_changes"]
    fig, ax = plt.subplots(figsize=(11, 4.5))

    ax.plot(run["ts"], run["latency_p95_ms"], color="#1f77b4", linewidth=1.5,
            label="latency p95 (ms)")
    ax.plot(run["ts"], run["latency_p50_ms"], color="#aec7e8", linewidth=1,
            linestyle=":", label="latency p50 (ms)")
    ax.set_ylabel("latency (ms)")

    if ups is not None and len(ups) > 1:
        for _, u in ups.iterrows():
            ax.axvline(u["ts"], color="#ff7f0e", linestyle="-", alpha=0.6, linewidth=1.2)
        ax.set_title(f"Per-second p95 + upstream.conf rewrites ({len(ups)} snapshots)")
    else:
        ax.set_title("Per-second p95 — no upstream.conf rewrites during run "
                     "(see #164 for why)")

    _annotate_phases(ax, data, y_text=0.92)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S", tz=run["ts"].dt.tz))
    ax.legend(loc="upper right")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


# ── plot 4: phase D anomaly recovery ─────────────────────────────────────────

def plot_anomaly_recovery(data: dict, out_path: Path) -> None:
    run    = data["run"]
    audit  = data["scaling_audit"]
    manifest = data["manifest"]
    injections = manifest.get("injections") or []

    fig, (ax_latency, ax_pool) = plt.subplots(
        2, 1, figsize=(11, 6), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )

    # Pick a window straddling phase D for the close-up
    phases = manifest["phases"]
    bench_start = run["ts"].iloc[0]
    window_start = bench_start + pd.Timedelta(seconds=phases["PHASE_B_END_SECS"])
    window_end   = bench_start + pd.Timedelta(seconds=phases["PHASE_E_END_SECS"])
    w = run[(run["ts"] >= window_start) & (run["ts"] <= window_end)]

    ax_latency.plot(w["ts"], w["latency_p95_ms"], color="#1f77b4", linewidth=1.5,
                    label="latency p95")
    ax_latency.plot(w["ts"], w["latency_p99_ms"], color="#d62728", linewidth=1,
                    linestyle="--", label="latency p99")
    ax_latency.set_ylabel("latency (ms)")
    ax_latency.set_title("Phase D anomaly window — latency + pool size")

    ax_pool.step(w["ts"], w["pool_size_active"], where="post",
                 color="#2ca02c", linewidth=2)
    ax_pool.set_ylabel("pool size")
    ax_pool.set_ylim(0, max(6, w["pool_size_active"].max() + 1))

    # Anomaly-injection markers
    for inj in injections:
        try:
            ts = pd.to_datetime(inj["injected_at"], utc=True)
        except (KeyError, ValueError):
            continue
        ax_latency.axvline(ts, color="#d62728", linestyle="-", alpha=0.6, linewidth=1.5)
        ax_latency.annotate(
            f"inject\n{inj.get('target', '?')}",
            xy=(ts, 0.95), xycoords=("data", "axes fraction"),
            ha="center", va="top", fontsize=8, color="#d62728",
        )
        rec = inj.get("recovered_at")
        if rec:
            rec_ts = pd.to_datetime(rec, utc=True)
            ax_latency.axvline(rec_ts, color="#2ca02c", linestyle="-", alpha=0.6, linewidth=1.5)
            ax_latency.annotate(
                "recover",
                xy=(rec_ts, 0.95), xycoords=("data", "axes fraction"),
                ha="center", va="top", fontsize=8, color="#2ca02c",
            )

    # Scaling markers within the window
    if audit is not None and not audit.empty:
        in_window = audit[(audit["ts"] >= window_start) & (audit["ts"] <= window_end)]
        for _, row in in_window.iterrows():
            ax_pool.axvline(row["ts"], color="orange", linestyle="--", alpha=0.7)
            ax_pool.annotate(
                f"{row['action']}",
                xy=(row["ts"], 0.95), xycoords=("data", "axes fraction"),
                ha="center", va="top", fontsize=8, color="orange",
            )

    ax_latency.legend(loc="upper right")
    ax_pool.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S", tz=run["ts"].dt.tz))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


# ── shared phase annotation ───────────────────────────────────────────────────

def _annotate_phases(ax, data: dict, *, y_text: float) -> None:
    """Mark phase boundaries as dotted verticals so every plot can be read in
    the same time vocabulary."""
    run = data["run"]
    phases = data["manifest"]["phases"]
    bench_start = run["ts"].iloc[0]
    boundaries = [
        ("A→B", phases["PHASE_A_END_SECS"]),
        ("B→C", phases["PHASE_B_END_SECS"]),
        ("C→D", phases["PHASE_C_END_SECS"]),
        ("D→E", phases["PHASE_D_END_SECS"]),
    ]
    for label, secs in boundaries:
        t = bench_start + pd.Timedelta(seconds=secs)
        ax.axvline(t, color="gray", linestyle=":", alpha=0.4, linewidth=1)
        ax.annotate(label, xy=(t, y_text), xycoords=("data", "axes fraction"),
                    ha="center", va="top", fontsize=8, color="gray")


# ── SUMMARY.md generator ──────────────────────────────────────────────────────

def write_summary(data: dict, out_path: Path) -> None:
    run      = data["run"]
    audit    = data["scaling_audit"]
    forecasts = data["forecasts"]
    injections = data["manifest"].get("injections") or []
    phases    = data["manifest"]["phases"]

    bench_start = run["ts"].iloc[0]
    bench_end   = run["ts"].iloc[-1]

    # Phase summaries
    phase_rows: list[str] = [
        "| Phase | Wall-clock window | User-count target | Observed RPS p50 | p95 latency (ms) | Pool size (min..max) |",
        "|---|---|---|---|---|---|",
    ]
    for phase in ["A_bootstrap", "B_forecast_burst", "C_sustain",
                  "D_anomaly_scale_down", "E_steady"]:
        sub = run[run["phase"] == phase]
        if sub.empty:
            phase_rows.append(f"| `{phase}` | — | — | — | — | — |")
            continue
        # phase-window
        win = f"{sub['ts'].iloc[0].strftime('%H:%M:%S')} → {sub['ts'].iloc[-1].strftime('%H:%M:%S')}"
        rps_p50 = sub["rps"].median()
        lat_p95 = sub["latency_p95_ms"].median()
        pool_lo = int(sub["pool_size_active"].min()) if sub["pool_size_active"].notna().any() else 0
        pool_hi = int(sub["pool_size_active"].max()) if sub["pool_size_active"].notna().any() else 0
        phase_rows.append(
            f"| `{phase}` | {win} | {int(sub['user_count'].max())} users | "
            f"{rps_p50:.1f} | {lat_p95:.0f} | {pool_lo}..{pool_hi} |"
        )

    # Time-to-react table
    ttr_rows = ["| # | Forecast time | Predicted RPS | First action | Action time | Delay |",
                "|---|---|---|---|---|---|"]
    if forecasts is not None and not forecasts.empty and audit is not None and not audit.empty:
        for i, fc in enumerate(forecasts.itertuples()):
            after = audit[audit["ts"] >= fc.captured_at]
            if after.empty:
                ttr_rows.append(f"| f{i} | {fc.captured_at.strftime('%H:%M:%S')} | "
                                f"{fc.predicted_rps:.1f} | — | — | — |")
                continue
            first = after.iloc[0]
            delay = (first["ts"] - fc.captured_at).total_seconds()
            ttr_rows.append(
                f"| f{i} | {fc.captured_at.strftime('%H:%M:%S')} | "
                f"{fc.predicted_rps:.1f} | `{first['action']}` (ic={int(first['instance_count'])}) | "
                f"{first['ts'].strftime('%H:%M:%S')} | {delay:.1f}s |"
            )
    else:
        ttr_rows.append("| — | no forecast↔action pairs in window | | | | |")

    # Anomaly recovery
    anomaly_rows = ["| Target | Injected | Recovered | Window | Pool when injected |",
                    "|---|---|---|---|---|"]
    for inj in injections:
        try:
            ts_inj = pd.to_datetime(inj["injected_at"], utc=True)
        except Exception:
            continue
        rec = inj.get("recovered_at")
        ts_rec = pd.to_datetime(rec, utc=True) if rec else None
        window = f"{(ts_rec - ts_inj).total_seconds():.0f}s" if ts_rec is not None else "—"
        pool_at = run[run["ts"] <= ts_inj]["pool_size_active"]
        pool_at_inj = int(pool_at.iloc[-1]) if not pool_at.empty else 0
        anomaly_rows.append(
            f"| `{inj.get('target', '?')}` (dynamic={inj.get('is_dynamic')}) | "
            f"{ts_inj.strftime('%H:%M:%S')} | "
            f"{ts_rec.strftime('%H:%M:%S') if ts_rec is not None else '—'} | "
            f"{window} | {pool_at_inj} backends |"
        )

    # Action counts
    action_counts = audit["action"].value_counts().to_dict() if (audit is not None and not audit.empty) else {}

    body = f"""# Adaptive-bench R2 → R3 SUMMARY — {data['manifest']['timestamp_utc']}

> Bench version `{data['manifest']['bench_version']}` · short={data['manifest']['short']} · git `{data['manifest'].get('git_sha', '?')[:8]}` ({data['manifest'].get('git_state', '?')})

Run anchor: **{bench_start.strftime('%Y-%m-%d %H:%M:%S UTC')} → {bench_end.strftime('%H:%M:%S UTC')}**  ({(bench_end - bench_start).total_seconds():.0f} s).

## Per-phase

{chr(10).join(phase_rows)}

## Time-to-react (forecast publish → first autoscaler action)

{chr(10).join(ttr_rows)}

## Phase-D anomaly window

{chr(10).join(anomaly_rows)}

## Autoscaler action counts (bench window)

- **scale_out**: {action_counts.get("scale_out", 0)}
- **scale_in**:  {action_counts.get("scale_in", 0)}
- **total decisions in audit**: {len(audit) if audit is not None else 0}

## Acceptance gates (#157 R3 § "Plots show")

- **Pool grew during Phase B in response to forecast** — _{ _b_gate_string(data) }_
- **Pool shrank during Phase D in response to load drop** — _{ _d_gate_string(data) }_
- **Anomaly isolation latency ≤ 2 s** — _{ _isolate_gate_string(data) }_

## Observed gaps surfaced by this run

- **#163** Decision-plane run loops die silently after multi-day uptime. Surfaced on the first R2 run when all four decision services showed `ticks_total: 0` despite `runloop_enabled: true`. Restarting them brought the chain back online; this run was produced with all services freshly restarted.
- **#164** lb-sidecar doesn't subscribe to `smartload.scale`. `upstream.conf` therefore did not change during the bench despite two scaling decisions. NGINX continued to route to the seed-list backends and relied on its own passive `max_fails` check to handle the scaled-down container. This is why `plot_upstream_timeline.png` shows no upstream-rewrite markers.

## Files in this directory

| File | Source |
|---|---|
| `MANIFEST.json` | run.py |
| `pre_status.json` / `post_status.json` | `GET /api/v1/status` |
| `scaling_audit.json` | `GET /api/v1/audit/scaling?limit=200` |
| `prom_timeseries.parquet` | prom_collector (1 Hz) |
| `decision_envelopes.jsonl` | sse_collector |
| `upstream_changes.jsonl` | upstream_watcher (2 s) |
| `locust_stats.csv`, `locust_stats_history.csv` | locust --csv-full-history |
| `run.parquet` + `forecasts/anomalies/scalings/routings/scaling_audit.parquet` | **join_run.py** (R3) |
| `plot_pool_size.png`, `plot_time_to_react.png`, `plot_upstream_timeline.png`, `plot_anomaly_recovery.png` | **plot_results.py** (R3) |
| `SUMMARY.md` | **this file** (R3) |
"""
    out_path.write_text(body, encoding="utf-8")


def _b_gate_string(data: dict) -> str:
    run   = data["run"]
    b     = run[run["phase"] == "B_forecast_burst"]
    if b.empty or b["pool_size_active"].isna().all():
        return "no phase-B data — gate cannot be evaluated"
    pre_b = run[run["phase"] == "A_bootstrap"]
    pre_max = int(pre_b["pool_size_active"].max()) if not pre_b.empty else 0
    b_max   = int(b["pool_size_active"].max())
    if b_max > pre_max:
        return f"yes — pool grew {pre_max}→{b_max} during B"
    return (f"no — pool stayed at {b_max} through B (likely #164: scale events "
            f"don't propagate to upstream.conf, so the actual Docker pool may "
            f"have moved but NGINX's view did not)")


def _d_gate_string(data: dict) -> str:
    run = data["run"]
    d   = run[run["phase"] == "D_anomaly_scale_down"]
    if d.empty or d["pool_size_active"].isna().all():
        return "no phase-D data — gate cannot be evaluated"
    pre_d = run[run["phase"] == "C_sustain"]
    pre_max = int(pre_d["pool_size_active"].max()) if not pre_d.empty else 0
    d_min   = int(d["pool_size_active"].min())
    if d_min < pre_max:
        return f"yes — pool shrank {pre_max}→{d_min} during D"
    return f"no — pool stayed at {d_min} through D (see #164)"


def _isolate_gate_string(data: dict) -> str:
    injections = data["manifest"].get("injections") or []
    if not injections:
        return "no injections recorded"
    inj = injections[0]
    if not inj.get("isolate_published"):
        return "isolate publish failed — gate cannot be evaluated"
    return ("anomaly POST and recovery POST both returned 200; precise latency "
            "between the published AnomalyEvent and the LB stopping traffic to "
            "the target requires #164 to land so upstream.conf rewrites are "
            "observable")


# ── multi-run error-band plots (#160) ─────────────────────────────────────────

def _discover_runs(batch_dir: Path) -> list[Path]:
    runs = sorted(p for p in batch_dir.glob("run-*") if p.is_dir())
    return runs or [batch_dir]


def _aligned_band(run_dirs: list[Path], value_col: str, max_secs: float):
    """Align each run's per-second `value_col` onto a common integer
    seconds-since-start grid, then return (grid, mean, ci_lower, ci_upper).

    With a single run the CI is undefined, so the band collapses to the mean
    line — i.e. it degrades exactly to the pre-#160 single-line plot."""
    grid = list(range(0, int(max_secs) + 1))
    series: list[pd.Series] = []
    for rd in run_dirs:
        p = rd / "run.parquet"
        if not p.exists():
            continue
        df = pd.read_parquet(p)
        if "seconds_since_start" not in df.columns or value_col not in df.columns:
            continue
        s = df.dropna(subset=["seconds_since_start"]).copy()
        s["sec"] = s["seconds_since_start"].round().astype(int)
        col = s.groupby("sec")[value_col].mean().reindex(grid).interpolate(limit_direction="both")
        series.append(col)
    if not series:
        return grid, None, None, None

    mat = pd.concat(series, axis=1)
    means, los, his = [], [], []
    for _, vals in mat.iterrows():
        st = bench_stats.mean_ci(vals.tolist())
        m = st["mean"]
        lo = m if math.isnan(st["ci_lower"]) else st["ci_lower"]
        hi = m if math.isnan(st["ci_upper"]) else st["ci_upper"]
        means.append(m); los.append(lo); his.append(hi)
    return grid, means, los, his


def _annotate_phase_secs(ax, phases: dict, y_text: float = 0.92) -> None:
    for label, key in (("A→B", "PHASE_A_END_SECS"), ("B→C", "PHASE_B_END_SECS"),
                       ("C→D", "PHASE_C_END_SECS"), ("D→E", "PHASE_D_END_SECS")):
        secs = phases.get(key)
        if secs is None:
            continue
        ax.axvline(secs, color="gray", linestyle=":", alpha=0.4, linewidth=1)
        ax.annotate(label, xy=(secs, y_text), xycoords=("data", "axes fraction"),
                    ha="center", va="top", fontsize=8, color="gray")


def _band_plot(run_dirs, phases, specs, title, ylabel, out_path, n_runs):
    """Draw one or more mean±CI bands (specs: list of (col, colour, label))."""
    max_secs = phases.get("PHASE_E_END_SECS", 360)
    fig, ax = plt.subplots(figsize=(11, 4.5))
    drew = False
    for col, colour, label in specs:
        grid, mean, lo, hi = _aligned_band(run_dirs, col, max_secs)
        if mean is None:
            continue
        ax.plot(grid, mean, color=colour, linewidth=1.8, label=label)
        if n_runs > 1:
            ax.fill_between(grid, lo, hi, color=colour, alpha=0.20)
        drew = True
    if not drew:
        ax.text(0.5, 0.5, "no run.parquet data", ha="center", va="center",
                transform=ax.transAxes, fontsize=11)
        ax.set_axis_off()
    else:
        _annotate_phase_secs(ax, phases)
        ci_note = " (mean ± 95% CI band)" if n_runs > 1 else " (single run)"
        ax.set_title(title + ci_note)
        ax.set_xlabel("seconds since shape start")
        ax.set_ylabel(ylabel)
        ax.legend(loc="upper right")
        ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def plot_phase_latency_ci(batch_dir: Path, out_path: Path) -> None:
    """Bar chart of per-phase p50/p95/p99 mean with CI error bars, read straight
    from summary.parquet."""
    sp = batch_dir / "summary.parquet"
    fig, ax = plt.subplots(figsize=(11, 4.5))
    if not sp.exists():
        ax.text(0.5, 0.5, "summary.parquet missing", ha="center", va="center",
                transform=ax.transAxes); ax.set_axis_off()
        fig.savefig(out_path, dpi=120); plt.close(fig); return

    summary = pd.read_parquet(sp)
    phase_order = ["A_bootstrap", "B_forecast_burst", "C_sustain",
                   "D_anomaly_scale_down", "E_steady"]
    metrics = [("latency_p50_ms", "#aec7e8", "p50"),
               ("latency_p95_ms", "#1f77b4", "p95"),
               ("latency_p99_ms", "#d62728", "p99")]
    phases_present = [p for p in phase_order if not summary[summary["phase"] == p].empty]
    x = list(range(len(phases_present)))
    width = 0.25
    for i, (metric, colour, label) in enumerate(metrics):
        means, errs = [], []
        for p in phases_present:
            row = summary[(summary["phase"] == p) & (summary["metric"] == metric)]
            if row.empty:
                means.append(0.0); errs.append(0.0); continue
            r = row.iloc[0]
            means.append(float(r["mean"]))
            hw = float(r["half_width"])
            errs.append(0.0 if math.isnan(hw) else hw)
        offset = (i - 1) * width
        ax.bar([xi + offset for xi in x], means, width=width, color=colour, label=label,
               yerr=errs, capsize=3, error_kw={"alpha": 0.7})
    ax.set_xticks(x); ax.set_xticklabels(phases_present, fontsize=8, rotation=10)
    ax.set_ylabel("latency (ms)")
    ax.set_title("Per-phase latency mean ± 95% CI (across runs)")
    ax.legend(loc="upper right"); ax.grid(axis="y", alpha=0.3)
    fig.tight_layout(); fig.savefig(out_path, dpi=120); plt.close(fig)


def plot_batch(batch_dir: Path) -> None:
    """Render the batch-level plots at the top of the batch directory.

    Banded (mean ± CI across runs): pool size, latency p50/p95.
    Event-overlay plots (time-to-react, anomaly recovery) use the first run as
    a representative timeline since they annotate discrete events, not bands.
    Plus a per-phase latency CI bar chart from summary.parquet."""
    run_dirs = _discover_runs(batch_dir)
    n_runs = len(run_dirs)
    manifest = _read_first_manifest(run_dirs)
    phases = manifest.get("phases", {})

    print(f"[plot] batch {batch_dir.name} — {n_runs} run(s)")
    _band_plot(run_dirs, phases,
               [("pool_size_active", "#2ca02c", "active backends")],
               "Adaptive-bench pool size", "pool size (active backends)",
               batch_dir / "plot_pool_size.png", n_runs)
    print("[plot] plot_pool_size.png")
    _band_plot(run_dirs, phases,
               [("latency_p95_ms", "#1f77b4", "p95 latency"),
                ("latency_p50_ms", "#aec7e8", "p50 latency")],
               "Per-second latency", "latency (ms)",
               batch_dir / "plot_upstream_timeline.png", n_runs)
    print("[plot] plot_upstream_timeline.png")
    plot_phase_latency_ci(batch_dir, batch_dir / "plot_phase_latency_ci.png")
    print("[plot] plot_phase_latency_ci.png")

    # Representative event-overlay plots from the first run that has run.parquet.
    rep = next((rd for rd in run_dirs if (rd / "run.parquet").exists()), None)
    if rep is not None:
        data = _load_run(rep)
        if data["run"] is not None:
            plot_time_to_react(data, batch_dir / "plot_time_to_react.png")
            print("[plot] plot_time_to_react.png (run-01 representative)")
            plot_anomaly_recovery(data, batch_dir / "plot_anomaly_recovery.png")
            print("[plot] plot_anomaly_recovery.png (run-01 representative)")


def _read_first_manifest(run_dirs: list[Path]) -> dict:
    for rd in run_dirs:
        mp = rd / "MANIFEST.json"
        if mp.exists():
            try:
                return json.loads(mp.read_text())
            except json.JSONDecodeError:
                continue
    return {}


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="adaptive-bench plot_results (#157/#160)")
    parser.add_argument("run_dir", help="A batch directory (multi-run) or a single run directory.")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        print(f"ERROR: {run_dir} is not a directory", file=sys.stderr)
        return 1

    # Batch directory (has run-* subfolders) → error-band plots.
    if any(p.is_dir() for p in run_dir.glob("run-*")):
        plot_batch(run_dir)
        return 0

    # Single run directory → legacy per-run plots + SUMMARY.md.
    data = _load_run(run_dir)
    if data["run"] is None:
        print(f"ERROR: run.parquet missing — run join_run.py first", file=sys.stderr)
        return 1

    print(f"[plot] {run_dir.name}")
    plot_pool_size(data,         run_dir / "plot_pool_size.png");        print("[plot] plot_pool_size.png")
    plot_time_to_react(data,     run_dir / "plot_time_to_react.png");    print("[plot] plot_time_to_react.png")
    plot_upstream_timeline(data, run_dir / "plot_upstream_timeline.png"); print("[plot] plot_upstream_timeline.png")
    plot_anomaly_recovery(data,  run_dir / "plot_anomaly_recovery.png"); print("[plot] plot_anomaly_recovery.png")
    write_summary(data, run_dir / "SUMMARY.md");                          print("[plot] SUMMARY.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
