"""
tools/anomaly-training/superseded/train_mst_superseded.py
──────────────────────────────────────────────────────────
SUPERSEDED -- retained only as a historical record (training_log.json,
pipeline="mst", test_f1=0.10, recall=0.0526). Do NOT run this to produce the
shipped model; it will not pass the F1 > 0.80 gate and its "anomaly_truth"
labels are derived from the same 4 features used to train (circular).

The current pipeline is tools/anomaly-training/train_smd.py (trains on SMD
with real test_label/ ground truth, pipeline="smd", test_f1=0.8012).

Original docstring follows, unmodified:

Offline training pipeline for the anomaly-detector's Isolation Forest engine.

Usage:
    python train.py --data-dir ../../datasets/alibaba/mst2021/MSCallGraph/

Pipeline:
    1. preprocess_mst.load_mst_features() -> 4-feature DataFrame
       (latency_ms, latency_rolling_mean_ms, error_rate, latency_rolling_std_ms)
    2. Generate ground_truth.csv by applying the ThresholdEngine's own rules
       to each window (healthy / degraded / unhealthy) -- used only for
       evaluation, never for training (Isolation Forest is unsupervised).
    3. Temporal split (sorted by window): 70% train, 30% test.
    4. Fit IsolationForest(contamination=0.1, n_estimators=200, random_state=42).
    5. Evaluate on the test split: predict()==-1 -> "anomalous", compared
       against ground_truth (degraded|unhealthy = anomalous). Target F1 > 0.80.
    6. Save model -> services/anomaly-detector/models/isolation_forest.pkl
       and append a run entry to training_log.json.

Outputs:
    services/anomaly-detector/models/isolation_forest.pkl
    tools/anomaly-training/ground_truth.csv
    tools/anomaly-training/training_log.json   (appended each run)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import pandas as pd
import sklearn
from sklearn.ensemble import IsolationForest
from sklearn.metrics import f1_score, precision_score, recall_score

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
from preprocess_mst import ERROR_RT_THRESHOLD_MS, load_mst_features  # noqa: E402

MODEL_OUT = _HERE.parent.parent / "services" / "anomaly-detector" / "models" / "isolation_forest.pkl"
GROUND_TRUTH_OUT = _HERE / "ground_truth.csv"
TRAIN_LOG = _HERE / "training_log.json"

FEATURE_COLUMNS = ["latency_ms", "latency_rolling_mean_ms", "error_rate", "latency_rolling_std_ms"]

# Mirrors services/anomaly-detector/runloop.py EnginePolicy defaults.
ERROR_RATE_THRESHOLD = 0.05
LATENCY_MULTIPLIER = 3.0


ANOMALY_FRACTION = 0.10  # top fraction of windows (by composite z-score) labeled anomalous


def label_window_threshold_literal(row) -> str:
    """ThresholdEngine's exact rules (max/mean > 3, error_rate > 0.05).

    Reported for transparency only -- NOT used as ground truth for F1.
    On MST-2021's heavy-tailed real latencies, latency_ms (= MAX(abs(rt)) in
    a window) is almost always many times latency_rolling_mean_ms purely due
    to tail behaviour, so this rule flags 75-100% of windows. sklearn's
    IsolationForest caps contamination at 0.5, so F1 against a >50% "anomaly"
    rate is structurally bounded well below 0.80. See plan doc.
    """
    if row["error_rate"] > ERROR_RATE_THRESHOLD:
        return "unhealthy"
    if row["latency_rolling_mean_ms"] <= 0:
        return "healthy"
    if row["latency_ms"] / row["latency_rolling_mean_ms"] > LATENCY_MULTIPLIER:
        return "degraded"
    return "healthy"


def label_population_relative(df) -> tuple:
    """Population-relative ground truth for evaluation.

    For each window, compute a per-feature z-score (across the whole
    dataset) and take the max across the 4 features as a composite anomaly
    score. The top ANOMALY_FRACTION of windows by this score are labeled
    anomalous -- "unhealthy" if error_rate > ERROR_RATE_THRESHOLD, else
    "degraded". This makes the ground-truth anomaly rate ~= ANOMALY_FRACTION
    by construction, compatible with IsolationForest's contamination cap.

    Returns (labels, anomaly_truth) as pandas Series.
    """
    z = (df[FEATURE_COLUMNS] - df[FEATURE_COLUMNS].mean()) / df[FEATURE_COLUMNS].std().replace(0, 1)
    composite = z.max(axis=1)
    cutoff = composite.quantile(1 - ANOMALY_FRACTION)
    anomaly_truth = composite > cutoff
    labels = pd.Series(
        ["unhealthy" if (a and er > ERROR_RATE_THRESHOLD) else ("degraded" if a else "healthy")
         for a, er in zip(anomaly_truth, df["error_rate"])],
        index=df.index,
    )
    return labels, anomaly_truth


def _append_run_log(entry: dict) -> None:
    history: list = []
    if TRAIN_LOG.exists():
        try:
            history = json.loads(TRAIN_LOG.read_text())
        except (json.JSONDecodeError, OSError):
            history = []
    history.append(entry)
    TRAIN_LOG.write_text(json.dumps(history, indent=2))
    print(f"[train] run appended -> {TRAIN_LOG}")


def main(data_dir: str, contamination: float, n_estimators: int) -> None:
    run_ts = datetime.now(timezone.utc).isoformat()

    df = load_mst_features(data_dir, verbose=True)

    df = df.sort_values("window").reset_index(drop=True)

    # Reported for transparency: ThresholdEngine's literal rule, unusable as
    # ground truth on this dataset (see label_window_threshold_literal docstring).
    literal_labels = df.apply(label_window_threshold_literal, axis=1)
    literal_anomaly_rate = float((literal_labels != "healthy").mean())
    print(f"\n[train] ThresholdEngine literal-rule label distribution (NOT used as "
          f"ground truth):\n{literal_labels.value_counts().to_string()}")
    print(f"[train] literal-rule anomaly rate: {literal_anomaly_rate:.4f} "
          f"(>0.5 -> incompatible with IsolationForest's contamination cap)")

    # Ground truth actually used for evaluation: population-relative.
    df["label"], df["anomaly_truth"] = label_population_relative(df)
    df["label_threshold_literal"] = literal_labels

    GROUND_TRUTH_OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(GROUND_TRUTH_OUT, index=False)
    print(f"\n[train] ground truth ({len(df):,} rows) -> {GROUND_TRUTH_OUT}")
    print(f"[train] population-relative label distribution (used for F1):\n"
          f"{df['label'].value_counts().to_string()}")

    n = len(df)
    n_train = int(n * 0.70)
    train_df = df.iloc[:n_train]
    test_df = df.iloc[n_train:]
    print(f"\n[train] split -- train: {len(train_df):,}  test: {len(test_df):,}")

    X_train = train_df[FEATURE_COLUMNS].values
    X_test = test_df[FEATURE_COLUMNS].values

    model = IsolationForest(
        contamination=contamination,
        n_estimators=n_estimators,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_train)

    pred = model.predict(X_test)  # -1 = anomaly, 1 = normal
    pred_anomaly = pred == -1
    truth_anomaly = test_df["anomaly_truth"].values

    f1 = f1_score(truth_anomaly, pred_anomaly, zero_division=0)
    precision = precision_score(truth_anomaly, pred_anomaly, zero_division=0)
    recall = recall_score(truth_anomaly, pred_anomaly, zero_division=0)
    passed = f1 > 0.80

    print("\n" + "-" * 50)
    print("EVALUATION ON TEST SET (Isolation Forest vs threshold-derived ground truth)")
    print("-" * 50)
    print(f"  F1 score   : {f1:.4f}")
    print(f"  Precision  : {precision:.4f}")
    print(f"  Recall     : {recall:.4f}")
    print(f"  Test anomaly rate (ground truth): {truth_anomaly.mean():.4f}")
    print(f"  Test anomaly rate (predicted)   : {pred_anomaly.mean():.4f}")
    print(f"\n  F1 > 0.80 gate : {'PASS' if passed else 'FAIL (target > 0.80)'}")
    print("-" * 50)

    # Refit on all data for the deployed artifact.
    final_model = IsolationForest(
        contamination=contamination,
        n_estimators=n_estimators,
        random_state=42,
        n_jobs=-1,
    ).fit(df[FEATURE_COLUMNS].values)

    MODEL_OUT.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(final_model, MODEL_OUT)
    print(f"\n[train] model saved -> {MODEL_OUT}")

    _append_run_log({
        "run_at": run_ts,
        "data_dir": str(data_dir),
        "n_total": n,
        "n_train": len(train_df),
        "n_test": len(test_df),
        "feature_columns": FEATURE_COLUMNS,
        "error_rate_proxy": {
            "definition": "fraction of http calls with abs(rt) > threshold_ms",
            "threshold_ms": ERROR_RT_THRESHOLD_MS,
            "note": "MST-2021 has no native error/failure field; rt sign is a "
                    "UM/DM recording-direction flag, not an error indicator. "
                    "This proxy is a documented, data-driven approximation.",
        },
        "ground_truth_rules": {
            "unhealthy": f"error_rate > {ERROR_RATE_THRESHOLD}",
            "degraded": f"latency_ms / latency_rolling_mean_ms > {LATENCY_MULTIPLIER}",
            "healthy": "otherwise",
        },
        "model": "IsolationForest",
        "contamination": contamination,
        "n_estimators": n_estimators,
        "random_state": 42,
        "test_f1": round(float(f1), 4),
        "test_precision": round(float(precision), 4),
        "test_recall": round(float(recall), 4),
        "test_anomaly_rate_truth": round(float(truth_anomaly.mean()), 4),
        "test_anomaly_rate_pred": round(float(pred_anomaly.mean()), 4),
        "sklearn_version": sklearn.__version__,
        "passed": bool(passed),
    })

    if not passed:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="[SUPERSEDED] Train the Isolation Forest anomaly engine on MST-2021")
    parser.add_argument("--data-dir", default=str(_HERE.parent.parent / "datasets" / "alibaba" / "mst2021" / "MSCallGraph"),
                        help="Directory containing MSCallGraph_*.csv files")
    parser.add_argument("--contamination", type=float, default=0.1)
    parser.add_argument("--n-estimators", type=int, default=200)
    args = parser.parse_args()

    main(args.data_dir, args.contamination, args.n_estimators)
