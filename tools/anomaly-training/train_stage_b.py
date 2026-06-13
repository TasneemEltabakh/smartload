"""
tools/anomaly-training/train_stage_b.py
─────────────────────────────────────────────
Stage-B training: a single-domain IsolationForest trained directly on
labeled production features collected by collect_production_data.py.

Unlike train_smd.py (Stage A, SMD bootstrap):
  - One dataset, one StandardScaler, no cross-domain scaler reconciliation
    (fixes A1-A3).
  - A genuine temporal train/holdout split on the collection's `window`
    index -- no interleaving needed because collect_production_data.py's
    schedule guarantees both halves contain both classes (fixes A5).
  - contamination derived from the observed anomaly_truth rate in the train
    split, not searched (fixes A7).

The output bundle uses the SAME schema IsolationForestEngine already
validates -- smd_scaler and production_scaler both alias the single fitted
scaler -- so no engine.py changes are needed and all existing
test_engine.py tests remain valid against this bundle.

Relationship to train_production.py: the *shipped* bundle on main is produced
by train_production.py (issue #165, "Option 3"), which fits scaler + forest in
a single synthetic production-shape space and logs `pipeline="production_synthetic"`.
This Stage-B script is a parallel, real-data track: it trains on features
collected from the live stack via latency injection and logs
`pipeline="production_live"`. The two are complementary -- Stage-B serves as
a live-data validation / alternative-retrain path; it does not replace the
#165 recalibration as the default shipped model.

This is Stage B on top of Stage A (train_smd.py); promotion to
services/anomaly-detector/models/isolation_forest.pkl is a manual `cp`
after reviewing the printed before/after comparison (this script does not
auto-promote).

Usage:
    python tools/anomaly-training/train_stage_b.py
    python tools/anomaly-training/train_stage_b.py --data-dir datasets/smartload-live/<ts>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import IsolationForest
from sklearn.metrics import f1_score, precision_score, recall_score
from sklearn.preprocessing import StandardScaler

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from preprocess_smd import FEATURE_COLUMNS  # noqa: E402

MODEL_OUT = _HERE.parent.parent / "services" / "anomaly-detector" / "models" / "isolation_forest.pkl"
TRAIN_LOG = _HERE / "training_log.json"
LIVE_DATA_ROOT = _HERE.parent.parent / "datasets" / "smartload-live"

N_ESTIMATORS = 200
RANDOM_STATE = 42
TRAIN_FRACTION = 0.70


def _latest_data_dir() -> Path:
    candidates = sorted(p for p in LIVE_DATA_ROOT.glob("*") if (p / "production_features.csv").exists())
    if not candidates:
        raise FileNotFoundError(
            f"No production_features.csv found under {LIVE_DATA_ROOT}. "
            f"Run collect_production_data.py first."
        )
    return candidates[-1]


def _append_run_log(entry: dict) -> None:
    history: list = []
    if TRAIN_LOG.exists():
        try:
            history = json.loads(TRAIN_LOG.read_text())
        except (json.JSONDecodeError, OSError):
            history = []
    history.append(entry)
    TRAIN_LOG.write_text(json.dumps(history, indent=2))
    print(f"[train_stage_b] run appended -> {TRAIN_LOG}")


def _load_shipped_metadata() -> dict | None:
    if not MODEL_OUT.exists():
        return None
    try:
        bundle = joblib.load(MODEL_OUT)
        return bundle.get("metadata")
    except Exception:  # noqa: BLE001
        return None


def main(data_dir: str | None) -> None:
    run_ts = datetime.now(timezone.utc).isoformat()
    src_dir = Path(data_dir).resolve() if data_dir else _latest_data_dir()
    csv_path = src_dir / "production_features.csv"
    df = pd.read_csv(csv_path)
    print(f"[train_stage_b] loaded {len(df)} rows from {csv_path}")

    n_windows = int(df["window"].max()) + 1
    split_window = int(n_windows * TRAIN_FRACTION)
    train_df = df[df["window"] < split_window].reset_index(drop=True)
    holdout_df = df[df["window"] >= split_window].reset_index(drop=True)
    print(
        f"[train_stage_b] temporal split @ window {split_window}/{n_windows}: "
        f"train={len(train_df)} rows ({train_df['anomaly_truth'].sum()} anomalous), "
        f"holdout={len(holdout_df)} rows ({holdout_df['anomaly_truth'].sum()} anomalous)"
    )

    if train_df["anomaly_truth"].sum() == 0 or holdout_df["anomaly_truth"].sum() == 0:
        print(
            "[train_stage_b] ERROR: train or holdout split has zero anomalous rows -- "
            "collection schedule did not produce a usable split.",
            file=sys.stderr,
        )
        sys.exit(1)

    scaler = StandardScaler().fit(train_df[FEATURE_COLUMNS].values)
    X_train = scaler.transform(train_df[FEATURE_COLUMNS].values)
    X_holdout = scaler.transform(holdout_df[FEATURE_COLUMNS].values)

    contamination = float(np.clip(train_df["anomaly_truth"].mean(), 0.01, 0.5))
    print(f"[train_stage_b] contamination (observed train anomaly rate, clipped): {contamination:.4f}")

    model = IsolationForest(
        contamination=contamination, n_estimators=N_ESTIMATORS, random_state=RANDOM_STATE, n_jobs=-1
    ).fit(X_train)

    pred = model.predict(X_holdout) == -1
    truth = holdout_df["anomaly_truth"].astype(bool).values
    f1 = f1_score(truth, pred, zero_division=0)
    precision = precision_score(truth, pred, zero_division=0)
    recall = recall_score(truth, pred, zero_division=0)

    print("\n" + "-" * 50)
    print("HOLDOUT EVALUATION (Isolation Forest vs injection-based ground truth)")
    print("-" * 50)
    print(f"  F1 score   : {f1:.4f}")
    print(f"  Precision  : {precision:.4f}")
    print(f"  Recall     : {recall:.4f}")
    print(f"  Holdout anomaly rate (truth): {truth.mean():.4f}")
    print(f"  Holdout anomaly rate (pred) : {pred.mean():.4f}")
    print("-" * 50)

    # Thresholds derived from the train-only model's decision_function on
    # holdout (pre-refit -- decision_function on a model's own training data
    # is optimistic). Single domain throughout: no cross-scaler mismatch (A3).
    #
    # collect_production_data.py's alternating schedule (is_anomalous_phase)
    # makes ~50% of all rows anomalous by construction, so `contamination` is
    # ~0.5 here (unlike Stage A's ~0.005). train_smd.py's healthy_above=P50 /
    # unhealthy_below=P(contamination*100) formula degenerates to
    # healthy_above == unhealthy_below at contamination=0.5 (no "degraded"
    # band at all). Instead, split the tails symmetrically around the median
    # so "degraded" covers the middle (1 - contamination) mass and
    # "healthy"/"unhealthy" are the outer contamination/2 tails each -- a
    # non-degenerate split for any contamination in (0, 1).
    holdout_scores = model.decision_function(X_holdout)
    healthy_above = float(np.percentile(holdout_scores, 100 * (1 - contamination / 2)))
    unhealthy_below = float(np.percentile(holdout_scores, 100 * (contamination / 2)))
    unhealthy_score_scale = float(unhealthy_below - holdout_scores.min())
    if unhealthy_score_scale <= 0:
        unhealthy_score_scale = 0.5

    print("\n[train_stage_b] derived thresholds:")
    print(f"  healthy_above         = {healthy_above:.4f}")
    print(f"  unhealthy_below       = {unhealthy_below:.4f}")
    print(f"  unhealthy_score_scale = {unhealthy_score_scale:.4f}")

    # Refit on all collected data for the deployed artifact.
    all_df = pd.concat([train_df, holdout_df], ignore_index=True)
    final_scaler = StandardScaler().fit(all_df[FEATURE_COLUMNS].values)
    final_model = IsolationForest(
        contamination=contamination, n_estimators=N_ESTIMATORS, random_state=RANDOM_STATE, n_jobs=-1
    ).fit(final_scaler.transform(all_df[FEATURE_COLUMNS].values))

    try:
        source_data_dir = str(src_dir.relative_to(_HERE.parent.parent)).replace("\\", "/")
    except ValueError:
        # --data-dir points outside the repo (e.g. an ad-hoc collection run) --
        # fall back to the absolute path rather than crashing.
        source_data_dir = str(src_dir)

    bundle = {
        "model": final_model,
        # smd_scaler/production_scaler both alias the single fitted scaler --
        # IsolationForestEngine only reads production_scaler, but the bundle
        # schema (and feature_order check) is preserved unchanged.
        "smd_scaler": final_scaler,
        "production_scaler": final_scaler,
        "feature_order": FEATURE_COLUMNS,
        "thresholds": {
            "healthy_above": healthy_above,
            "unhealthy_below": unhealthy_below,
            "unhealthy_score_scale": unhealthy_score_scale,
        },
        "metadata": {
            "trained_at": run_ts,
            "pipeline": "production_live",
            "latency_only": True,
            "source_data_dir": source_data_dir,
            "n_phases": n_windows,
            "n_train_rows": len(train_df),
            "n_holdout_rows": len(holdout_df),
            "contamination": contamination,
            "n_estimators": N_ESTIMATORS,
            "random_state": RANDOM_STATE,
            "sklearn_version": sklearn.__version__,
            "test_f1": round(float(f1), 4),
            "test_precision": round(float(precision), 4),
            "test_recall": round(float(recall), 4),
            "test_anomaly_rate_truth": round(float(truth.mean()), 4),
            "test_anomaly_rate_pred": round(float(pred.mean()), 4),
        },
    }

    out_path = src_dir / "isolation_forest_production.pkl"
    joblib.dump(bundle, out_path)
    print(f"\n[train_stage_b] candidate bundle saved -> {out_path}")
    print(f"[train_stage_b] NOT auto-promoted. To promote: cp {out_path} {MODEL_OUT}")

    shipped = _load_shipped_metadata()
    if shipped:
        print("\n[train_stage_b] comparison vs currently-shipped bundle:")
        print(
            f"  shipped : pipeline={shipped.get('pipeline')} "
            f"f1={shipped.get('test_f1')} precision={shipped.get('test_precision')} "
            f"recall={shipped.get('test_recall')}"
        )
        print(f"  this run: pipeline=production_live f1={f1:.4f} precision={precision:.4f} recall={recall:.4f}")
    else:
        print("\n[train_stage_b] no shipped bundle found for comparison.")

    _append_run_log({**bundle["metadata"], "passed": True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train Stage-B IsolationForest on collected production features")
    parser.add_argument(
        "--data-dir", default=None, help="datasets/smartload-live/<timestamp>/ directory (default: latest)"
    )
    args = parser.parse_args()

    main(args.data_dir)
