"""
tools/anomaly-training/preprocess_mst.py
──────────────────────────────────────────
Load Alibaba MST-2021 MSCallGraph CSVs and produce the 4-feature dataset
matching services/anomaly-detector/engine_base.BackendFeatures:

    latency_ms, latency_rolling_mean_ms, error_rate, latency_rolling_std_ms

Per (backend = dm, 60-second window), over rpctype == "http" rows with a
known dm (excludes "(?)" / empty):

    latency_ms               = max(abs(rt))
    latency_rolling_mean_ms  = mean(abs(rt))
    latency_rolling_std_ms   = std(abs(rt))
    error_rate                = fraction of rows with abs(rt) > ERROR_RT_THRESHOLD_MS
    sample_count              = count

`rt` sign is a UM/DM recording-direction flag in MST-2021, not an error
indicator (no table in the dataset has a failure/status field) — see
the project plan for the full rationale. ERROR_RT_THRESHOLD_MS=500 is a
documented proxy: ~p95 of observed http abs(rt) in MSCallGraph_0.csv
(median 21ms, p95 442ms, p99 663ms).

Windows with sample_count < MIN_SAMPLE_COUNT are dropped, matching the
inference-time data-quality gate.

Usage:
    from preprocess_mst import load_mst_features
    df = load_mst_features("datasets/alibaba/mst2021/MSCallGraph/")
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

WINDOW_MS = 60_000
ERROR_RT_THRESHOLD_MS = 500.0
MIN_SAMPLE_COUNT = 10

MST_COLUMNS = ["idx", "traceid", "timestamp", "rpcid", "um", "rpctype", "dm", "interface", "rt"]


def _load_one(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(
        csv_path,
        header=0,
        names=MST_COLUMNS,
        usecols=["timestamp", "rpctype", "dm", "rt"],
        dtype={"timestamp": "int64", "rpctype": "category", "rt": "float64"},
    )
    df = df[(df["rpctype"] == "http") & df["dm"].notna() & (df["dm"] != "(?)") & (df["dm"] != "")]
    if df.empty:
        return df
    out = df[["dm", "timestamp", "rt"]].copy()
    out["abs_rt"] = out["rt"].abs()
    out["window"] = out["timestamp"] // WINDOW_MS
    return out[["dm", "window", "abs_rt"]]


def load_mst_features(data_dir: str | Path, *, verbose: bool = True) -> pd.DataFrame:
    """Load and aggregate all MSCallGraph_*.csv files in `data_dir`.

    Each file covers an independent 5-minute trace window (windows 0-4),
    so window indices are offset per file (i * 10_000) to keep
    (backend, window) groups from colliding across files.
    """
    data_dir = Path(data_dir)
    csv_files = sorted(data_dir.glob("MSCallGraph_*.csv")) if data_dir.is_dir() else [data_dir]
    if not csv_files:
        raise ValueError(f"No MSCallGraph CSV files found in {data_dir}")

    frames: list[pd.DataFrame] = []
    for i, csv_path in enumerate(csv_files):
        chunk = _load_one(csv_path)
        if verbose:
            print(f"[preprocess_mst] {csv_path.name}: {len(chunk):,} usable http rows")
        if not chunk.empty:
            chunk = chunk.copy()
            chunk["window"] = chunk["window"] + i * 10_000
            frames.append(chunk)

    if not frames:
        raise ValueError("No usable http rows found across all files")

    all_rows = pd.concat(frames, ignore_index=True)

    grouped = all_rows.groupby(["dm", "window"])["abs_rt"]
    features = grouped.agg(
        latency_ms="max",
        latency_rolling_mean_ms="mean",
        latency_rolling_std_ms="std",
        sample_count="count",
    ).reset_index()
    features["latency_rolling_std_ms"] = features["latency_rolling_std_ms"].fillna(0.0)

    error_rate = grouped.apply(lambda s: (s > ERROR_RT_THRESHOLD_MS).mean())
    features["error_rate"] = error_rate.values

    features = features[features["sample_count"] >= MIN_SAMPLE_COUNT].reset_index(drop=True)
    features = features.rename(columns={"dm": "backend_id"})

    if verbose:
        print(f"[preprocess_mst] (backend, window) groups with sample_count >= "
              f"{MIN_SAMPLE_COUNT}: {len(features):,}")
        print(f"[preprocess_mst] distinct backends: {features['backend_id'].nunique()}")
        print(f"[preprocess_mst] error_rate: min={features['error_rate'].min():.4f}, "
              f"mean={features['error_rate'].mean():.4f}, max={features['error_rate'].max():.4f}")
        print(f"[preprocess_mst] latency_ms: min={features['latency_ms'].min():.1f}, "
              f"mean={features['latency_ms'].mean():.1f}, max={features['latency_ms'].max():.1f}")

    return features[[
        "backend_id", "window", "latency_ms", "latency_rolling_mean_ms",
        "error_rate", "latency_rolling_std_ms", "sample_count",
    ]]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preview MST-2021 preprocessing")
    parser.add_argument("--data-dir", default="datasets/alibaba/mst2021/MSCallGraph/",
                        help="Directory containing MSCallGraph_*.csv files")
    args = parser.parse_args()

    df = load_mst_features(args.data_dir, verbose=True)
    print("\n-- First 5 rows --")
    print(df.head())
