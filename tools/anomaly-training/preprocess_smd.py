"""
tools/anomaly-training/preprocess_smd.py
──────────────────────────────────────────
Load the Server Machine Dataset (SMD / OmniAnomaly,
https://github.com/NetManAIOps/OmniAnomaly, vendored at datasets/smd/) and
produce the 4-feature dataset matching
services/anomaly-detector/engine_base.BackendFeatures:

    latency_ms, latency_rolling_mean_ms, error_rate, latency_rolling_std_ms

SMD ships per-machine `train/<machine>.txt` and `test/<machine>.txt`, each
38 comma-separated columns (no header) of metrics already min-max normalized
to [0, 1] *per machine*. `test_label/<machine>.txt` is one binary label per
row, aligned 1:1 with test/<machine>.txt, indicating a real (human-curated)
anomaly window.

IMPORTANT: these [0, 1] values are the dataset's own per-machine
normalization -- they are NOT in the same units as production
(`ANOMALY_QUERY` real-millisecond latency / [0,1] error fraction). This
module produces *training-distribution* data only. Reconciling SMD's scale
with production's scale is handled separately by the "production scaler"
fitted in train_smd.py from preprocess_mst.load_mst_features() output.

Column mapping (chosen empirically -- see train_smd.py's search over
candidate dims, justified by frequency in interpretation_label/ across all
28 machines):

    SMD column <latency_dim>          -> latency_ms              (raw value)
    SMD column <latency_dim>, rolling -> latency_rolling_mean_ms (trailing
                                          mean over <rolling_window> rows)
    SMD column <latency_dim>, rolling -> latency_rolling_std_ms  (trailing
                                          std over <rolling_window> rows,
                                          NaN at the start filled with 0.0)
    SMD column <error_dim>            -> error_rate              (raw value,
                                          already bounded [0, 1] in SMD)

Usage:
    from preprocess_smd import load_smd_machine, load_smd_pool
    train_df, test_df = load_smd_machine("datasets/smd", "machine-1-1",
                                           latency_dim=0, error_dim=12,
                                           rolling_window=5)
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

FEATURE_COLUMNS = ["latency_ms", "latency_rolling_mean_ms", "error_rate", "latency_rolling_std_ms"]

N_SMD_DIMS = 38


def read_smd_raw(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise ValueError(f"SMD file not found: {path}")
    df = pd.read_csv(path, header=None)
    if df.shape[1] != N_SMD_DIMS:
        raise ValueError(f"{path}: expected {N_SMD_DIMS} columns, got {df.shape[1]}")
    return df


def build_features(raw: pd.DataFrame, latency_dim: int, error_dim: int, rolling_window: int) -> pd.DataFrame:
    """Build the 4 canonical feature columns from a raw 38-column SMD frame."""
    latency = raw[latency_dim]
    return pd.DataFrame({
        "latency_ms": latency,
        "latency_rolling_mean_ms": latency.rolling(rolling_window, min_periods=1).mean(),
        "error_rate": raw[error_dim],
        "latency_rolling_std_ms": latency.rolling(rolling_window, min_periods=1).std().fillna(0.0),
    })


def load_smd_machine_raw(data_dir: str | Path, machine: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """Load one SMD machine's raw 38-column train/test frames + test labels.

    Returns (train_raw, test_raw, test_labels). Useful for callers (e.g. a
    hyperparameter search) that want to build features for many
    (latency_dim, error_dim, rolling_window) combos without re-reading the
    CSVs each time.
    """
    data_dir = Path(data_dir)
    train_raw = read_smd_raw(data_dir / "train" / f"{machine}.txt")
    test_raw = read_smd_raw(data_dir / "test" / f"{machine}.txt")

    label_path = data_dir / "test_label" / f"{machine}.txt"
    if not label_path.exists():
        raise ValueError(f"SMD label file not found: {label_path}")
    labels = pd.read_csv(label_path, header=None)[0]
    if len(labels) != len(test_raw):
        raise ValueError(f"{label_path}: {len(labels)} labels but test/{machine}.txt has {len(test_raw)} rows")

    return train_raw.reset_index(drop=True), test_raw.reset_index(drop=True), labels.astype(bool).reset_index(drop=True)


def load_smd_machine(
    data_dir: str | Path,
    machine: str,
    *,
    latency_dim: int,
    error_dim: int,
    rolling_window: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load one SMD machine's train/test split as 4-feature DataFrames.

    Returns (train_df, test_df). train_df has only FEATURE_COLUMNS (SMD's
    train/ split is documented as anomaly-free "normal" reference data).
    test_df additionally has an "anomaly_truth" bool column from
    test_label/<machine>.txt.
    """
    train_raw, test_raw, labels = load_smd_machine_raw(data_dir, machine)

    train_df = build_features(train_raw, latency_dim, error_dim, rolling_window)
    test_df = build_features(test_raw, latency_dim, error_dim, rolling_window)
    test_df["anomaly_truth"] = labels.reset_index(drop=True)

    return train_df.reset_index(drop=True), test_df.reset_index(drop=True)


def load_smd_pool(
    data_dir: str | Path,
    machines: list[str],
    *,
    latency_dim: int,
    error_dim: int,
    rolling_window: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load and concatenate multiple SMD machines.

    Each machine's rolling statistics are computed independently (no
    cross-machine leakage) before concatenation.
    """
    train_frames = []
    test_frames = []
    for machine in machines:
        train_df, test_df = load_smd_machine(
            data_dir, machine, latency_dim=latency_dim, error_dim=error_dim, rolling_window=rolling_window
        )
        train_frames.append(train_df)
        test_frames.append(test_df)

    return (
        pd.concat(train_frames, ignore_index=True),
        pd.concat(test_frames, ignore_index=True),
    )


def anomaly_rate(data_dir: str | Path, machine: str) -> float:
    """Fraction of test_label/<machine>.txt rows that are anomalous (1)."""
    labels = pd.read_csv(Path(data_dir) / "test_label" / f"{machine}.txt", header=None)[0]
    return float(labels.mean())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preview SMD preprocessing")
    parser.add_argument("--data-dir", default="datasets/smd")
    parser.add_argument("--machine", default="machine-1-1")
    parser.add_argument("--latency-dim", type=int, default=0)
    parser.add_argument("--error-dim", type=int, default=12)
    parser.add_argument("--rolling-window", type=int, default=5)
    args = parser.parse_args()

    train_df, test_df = load_smd_machine(
        args.data_dir, args.machine,
        latency_dim=args.latency_dim, error_dim=args.error_dim, rolling_window=args.rolling_window,
    )
    print(f"[preprocess_smd] {args.machine}: train={len(train_df):,} rows, test={len(test_df):,} rows")
    print(f"[preprocess_smd] anomaly_truth rate (test): {test_df['anomaly_truth'].mean():.4f}")
    print("\n-- train describe --")
    print(train_df[FEATURE_COLUMNS].describe())
    print("\n-- test describe --")
    print(test_df[FEATURE_COLUMNS].describe())
    print("\n-- First 5 test rows --")
    print(test_df.head())
