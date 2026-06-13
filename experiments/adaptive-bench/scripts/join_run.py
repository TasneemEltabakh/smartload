"""
experiments/adaptive-bench/scripts/join_run.py
───────────────────────────────────────────────
Round 3 join pipeline (#157). Reads the eight raw artefacts produced by
run.py and emits:

  run.parquet             — per-second joined timeline (locust + pool + forecast)
  forecasts.parquet       — every ForecastResult envelope captured during the bench
  anomalies.parquet       — every AnomalyEvent envelope captured during the bench
  scalings.parquet        — every ScalingEvent (from envelopes + audit cross-check)
  routings.parquet        — every RoutingRecommendation envelope
  pool_size.parquet       — pool-size time series derived from upstream snapshots

The primary timeline is the Locust per-second history (the densest signal).
Other streams are projected onto it via `pandas.merge_asof(..., direction="backward")`
so each second carries the most recent state from each upstream.

Usage:
  python experiments/adaptive-bench/scripts/join_run.py <results-dir>

The results-dir must be a single bench run directory (one of the
timestamped folders under `experiments/adaptive-bench/results/`).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable

import pandas as pd
import pyarrow.parquet as pq


# ── time helpers ──────────────────────────────────────────────────────────────

def _utc(series: pd.Series) -> pd.Series:
    """Coerce a series to UTC-aware datetime[ns]. Handles ISO 8601 strings,
    naive datetimes, and epoch seconds."""
    out = pd.to_datetime(series, utc=True, errors="coerce")
    return out


def _read_manifest(run_dir: Path) -> dict:
    return json.loads((run_dir / "MANIFEST.json").read_text())


# ── loaders ───────────────────────────────────────────────────────────────────

def load_locust_history(run_dir: Path) -> pd.DataFrame:
    """Locust history is per-second per-name. We keep only `Aggregated` rows
    so the primary index is one row per second covering the whole run."""
    path = run_dir / "locust_stats_history.csv"
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["Timestamp"], unit="s", utc=True)
    df = df[df["Name"] == "Aggregated"].copy()
    # Keep the columns R3 plots reach for; the rest can be retrieved from the
    # raw CSV if R4 wants them.
    keep = ["ts", "User Count", "Requests/s", "Failures/s",
            "50%", "95%", "99%", "Total Request Count",
            "Total Failure Count", "Total Average Response Time",
            "Total Max Response Time"]
    df = df[[c for c in keep if c in df.columns]].rename(columns={
        "User Count":               "user_count",
        "Requests/s":               "rps",
        "Failures/s":               "fps",
        "50%":                      "latency_p50_ms",
        "95%":                      "latency_p95_ms",
        "99%":                      "latency_p99_ms",
        "Total Request Count":      "total_requests",
        "Total Failure Count":      "total_failures",
        "Total Average Response Time": "latency_avg_ms",
        "Total Max Response Time":  "latency_max_ms",
    })
    df = df.sort_values("ts").reset_index(drop=True)
    return df


def load_sse_envelopes(run_dir: Path, bench_start: pd.Timestamp) -> pd.DataFrame:
    """Flatten decision_envelopes.jsonl into one row per envelope. Drops
    envelopes whose `captured_at` predates the bench start (backlog from
    previous runs the orchestrator's SSE collector caught on connect)."""
    rows: list[dict] = []
    path = run_dir / "decision_envelopes.jsonl"
    if not path.exists():
        return pd.DataFrame(columns=["captured_at", "channel", "source",
                                     "envelope_timestamp", "payload"])
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            outer = rec.get("envelope") or {}
            inner_meta = outer.get("envelope") or {}
            rows.append({
                "captured_at":        rec.get("captured_at"),
                "channel":            outer.get("channel"),
                "source":             inner_meta.get("source"),
                "envelope_timestamp": inner_meta.get("timestamp"),
                "payload":            outer.get("payload") or {},
            })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["captured_at"] = _utc(df["captured_at"])
    # Tail off pre-bench backlog
    df = df[df["captured_at"] >= bench_start].copy()
    df = df.sort_values("captured_at").reset_index(drop=True)
    return df


def split_envelopes_by_channel(envelopes: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Split the unified envelope frame into per-channel frames with flattened
    payload columns useful for plotting."""
    out: dict[str, pd.DataFrame] = {}

    # smartload.forecast
    f = envelopes[envelopes["channel"] == "smartload.forecast"].copy()
    if not f.empty:
        f["predicted_rps"]    = f["payload"].map(lambda p: p.get("predicted_rps"))
        f["horizon_minutes"]  = f["payload"].map(lambda p: p.get("horizon_minutes"))
        f["model_id"]         = f["payload"].map(lambda p: p.get("model_id"))
        f["confidence_lower"] = f["payload"].map(lambda p: p.get("confidence_lower"))
        f["confidence_upper"] = f["payload"].map(lambda p: p.get("confidence_upper"))
    out["forecasts"] = f.reset_index(drop=True)

    # smartload.anomaly
    a = envelopes[envelopes["channel"] == "smartload.anomaly"].copy()
    if not a.empty:
        a["backend_id"] = a["payload"].map(lambda p: p.get("backend_id"))
        a["status"]     = a["payload"].map(lambda p: p.get("status"))
        a["score"]      = a["payload"].map(lambda p: p.get("score"))
        a["reason"]     = a["payload"].map(
            lambda p: (p.get("features") or {}).get("reason") if isinstance(p.get("features"), dict) else None
        )
    out["anomalies"] = a.reset_index(drop=True)

    # smartload.scale
    s = envelopes[envelopes["channel"] == "smartload.scale"].copy()
    if not s.empty:
        s["action"]         = s["payload"].map(lambda p: p.get("action"))
        s["instance_count"] = s["payload"].map(lambda p: p.get("instance_count"))
        s["reason"]         = s["payload"].map(lambda p: p.get("reason"))
        s["mechanism"]      = s["payload"].map(lambda p: p.get("mechanism"))
    out["scalings"] = s.reset_index(drop=True)

    # smartload.routing
    r = envelopes[envelopes["channel"] == "smartload.routing"].copy()
    if not r.empty:
        r["mode"]       = r["payload"].map(lambda p: p.get("mode"))
        r["confidence"] = r["payload"].map(lambda p: p.get("confidence"))
        r["model_id"]   = r["payload"].map(lambda p: p.get("model_id"))
    out["routings"] = r.reset_index(drop=True)

    return out


# ── upstream.conf → pool-size timeseries ──────────────────────────────────────

_SERVER_RE = re.compile(r"^\s*server\s+(\S+?)\s+(.*?);", re.MULTILINE)
_DOWN_RE   = re.compile(r"^\s*server\s+\S+\s+.*\bdown\b.*;", re.MULTILINE)


def _count_active_servers(body: str) -> int:
    """Active servers = total server lines minus those marked `down`."""
    total = len(_SERVER_RE.findall(body))
    down  = len(_DOWN_RE.findall(body))
    return max(0, total - down)


def load_upstream_changes(run_dir: Path) -> pd.DataFrame:
    """One row per upstream.conf snapshot — ts + pool size + excluded count."""
    rows: list[dict] = []
    path = run_dir / "upstream_changes.jsonl"
    if not path.exists():
        return pd.DataFrame(columns=["ts", "pool_size_active", "excluded_count"])
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            body  = rec.get("body") or ""
            total = len(_SERVER_RE.findall(body))
            down  = len(_DOWN_RE.findall(body))
            rows.append({
                "ts":                rec.get("ts"),
                "pool_size_active":  max(0, total - down),
                "pool_size_total":   total,
                "excluded_count":    down,
            })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ts"] = _utc(df["ts"])
    df = df.sort_values("ts").reset_index(drop=True)
    return df


# ── scaling audit (server-side ground truth) ──────────────────────────────────

def load_scaling_audit(run_dir: Path, bench_start: pd.Timestamp,
                       bench_end: pd.Timestamp) -> pd.DataFrame:
    """Audit endpoint returns the most recent N events globally. Filter to
    just the bench window — this is the canonical scaling-decision record."""
    path = run_dir / "scaling_audit.json"
    if not path.exists():
        return pd.DataFrame(columns=["ts", "action", "instance_count", "reason"])
    raw = json.loads(path.read_text())
    events = raw if isinstance(raw, list) else (raw.get("events") or raw)
    df = pd.DataFrame(events)
    if df.empty or "time" not in df.columns:
        return pd.DataFrame(columns=["ts", "action", "instance_count", "reason"])
    df["ts"] = _utc(df["time"])
    df = df[(df["ts"] >= bench_start) & (df["ts"] <= bench_end)].copy()
    df = df[["ts", "action", "instance_count", "reason"]]
    return df.sort_values("ts").reset_index(drop=True)


# ── phase assignment ──────────────────────────────────────────────────────────

def assign_phase(df_with_ts: pd.DataFrame,
                 *,
                 bench_start: pd.Timestamp,
                 phases: dict) -> pd.Series:
    """Tag each row with the phase it falls into based on seconds-since-start."""
    sec = (df_with_ts["ts"] - bench_start).dt.total_seconds()
    out = pd.Series("pre_bench", index=df_with_ts.index, dtype=object)
    out = out.mask(sec.between(0, phases["PHASE_A_END_SECS"], inclusive="left"), "A_bootstrap")
    out = out.mask(sec.between(phases["PHASE_A_END_SECS"], phases["PHASE_B_END_SECS"], inclusive="left"), "B_forecast_burst")
    out = out.mask(sec.between(phases["PHASE_B_END_SECS"], phases["PHASE_C_END_SECS"], inclusive="left"), "C_sustain")
    out = out.mask(sec.between(phases["PHASE_C_END_SECS"], phases["PHASE_D_END_SECS"], inclusive="left"), "D_anomaly_scale_down")
    out = out.mask(sec.between(phases["PHASE_D_END_SECS"], phases["PHASE_E_END_SECS"], inclusive="left"), "E_steady")
    out = out.mask(sec >= phases["PHASE_E_END_SECS"], "post_bench")
    return out


# ── the join itself ───────────────────────────────────────────────────────────

def build_run(run_dir: Path) -> dict[str, pd.DataFrame]:
    manifest = _read_manifest(run_dir)
    phases   = manifest["phases"]

    # bench window — use the locust first/last tick as the canonical anchor
    locust = load_locust_history(run_dir)
    if locust.empty:
        raise RuntimeError("locust_stats_history.csv has no Aggregated rows")
    bench_start = locust["ts"].iloc[0]
    bench_end   = locust["ts"].iloc[-1]

    envelopes = load_sse_envelopes(run_dir, bench_start)
    by_channel = split_envelopes_by_channel(envelopes)

    upstream  = load_upstream_changes(run_dir)
    scaling_audit = load_scaling_audit(run_dir, bench_start, bench_end)

    # Build run.parquet — primary index is locust per-second
    primary = locust.copy()
    primary["phase"] = assign_phase(primary, bench_start=bench_start, phases=phases)
    primary["seconds_since_start"] = (primary["ts"] - bench_start).dt.total_seconds()

    # Attach latest forecast at each tick (forward fill)
    if not by_channel["forecasts"].empty:
        f = by_channel["forecasts"][["captured_at", "predicted_rps", "model_id"]].rename(
            columns={"captured_at": "ts",
                     "predicted_rps": "forecast_predicted_rps",
                     "model_id":      "forecast_model"}).sort_values("ts")
        primary = pd.merge_asof(primary, f, on="ts", direction="backward")

    # Attach latest pool state
    if not upstream.empty:
        u = upstream[["ts", "pool_size_active", "excluded_count"]].sort_values("ts")
        primary = pd.merge_asof(primary, u, on="ts", direction="backward")

    # Attach the latest routing mode
    if not by_channel["routings"].empty:
        r = by_channel["routings"][["captured_at", "mode", "confidence"]].rename(
            columns={"captured_at": "ts",
                     "mode":        "routing_mode",
                     "confidence":  "routing_confidence"}).sort_values("ts")
        primary = pd.merge_asof(primary, r, on="ts", direction="backward")

    return {
        "run":              primary,
        "forecasts":        by_channel["forecasts"],
        "anomalies":        by_channel["anomalies"],
        "scalings":         by_channel["scalings"],
        "routings":         by_channel["routings"],
        "upstream_changes": upstream,
        "scaling_audit":    scaling_audit,
    }


def _to_parquet_safe(df: pd.DataFrame, path: Path) -> None:
    """Write parquet; drop dict-typed columns that pyarrow can't infer."""
    drop = [c for c in df.columns if df[c].apply(lambda v: isinstance(v, dict)).any()]
    out = df.drop(columns=drop) if drop else df
    out.to_parquet(path, index=False)


def write_tables(run_dir: Path, tables: dict[str, pd.DataFrame]) -> None:
    """Write each joined table to `<run_dir>/<name>.parquet`, skipping empties.
    Shared by the CLI and the multi-run batch analyzer (#160)."""
    for name, df in tables.items():
        out = run_dir / f"{name}.parquet"
        if df.empty:
            print(f"[join] {name}: empty — skipped")
            continue
        _to_parquet_safe(df, out)
        print(f"[join] {name}: {len(df):>5} rows -> {out.name}")


def join_run_dir(run_dir: Path) -> dict[str, pd.DataFrame]:
    """Build + write the joined tables for one run directory; returns them."""
    print(f"[join] {run_dir.name}")
    tables = build_run(run_dir)
    write_tables(run_dir, tables)
    return tables


def main() -> int:
    parser = argparse.ArgumentParser(description="adaptive-bench R3 join_run (#157)")
    parser.add_argument("run_dir", help="A single bench run directory (results/<TIMESTAMP>/ or a run-NN/).")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        print(f"ERROR: {run_dir} is not a directory", file=sys.stderr)
        return 1

    join_run_dir(run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
