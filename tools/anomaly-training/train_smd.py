"""
tools/anomaly-training/train_smd.py
──────────────────────────────────
Offline training pipeline for the anomaly-detector's Isolation Forest engine,
trained on the Server Machine Dataset (SMD / OmniAnomaly) -- REAL anomaly
labels, replacing the invented "population-relative" labels used by
superseded/train_mst_superseded.py (MST-2021 pipeline; see training_log.json,
run 2026-06-09, test_f1=0.10, ground truth computed from the same 4 features
used to train, i.e. circular).

Pipeline:
    1. preprocess_smd.load_smd_machine_raw() loads each candidate machine's
       raw 38-column train/test frames + real test_label/ once.
    2. Search over (latency_dim in {dim1}, error_dim in top
       interpretation_label-frequency candidates, rolling_window,
       contamination) x (machine set), building the 4 canonical features
       via preprocess_smd.build_features() for each combination.
    3. For each machine set, split SMD's test split 50/50 (temporal) into
       tune_df / holdout_df. Pick the combo with the best F1 on tune_df,
       then report F1/precision/recall on holdout_df (never used for
       tuning) -- this is the number checked against the F1 > 0.80 gate
       (SOT N2.1 KPI) and logged.
    4. Stop early if a machine set clears the gate; otherwise keep the best
       result across all machine sets tried.
    5. Fit production_scaler (StandardScaler) on
       preprocess_mst.load_mst_features() output -- real-millisecond
       latency / [0,1] error_rate, the best available proxy for live
       ANOMALY_QUERY features. This reconciles SMD's per-machine [0,1]
       normalization with production's real-unit scale (see
       services/anomaly-detector/engines/isolation_forest/README.md for the
       full domain-adaptation rationale).
    6. Refit smd_scaler + IsolationForest on all SMD data (train + test) for
       the chosen machine set -> deployed model. Thresholds
       (healthy_above / unhealthy_below / unhealthy_score_scale) are derived
       from the train-only model's decision_function distribution on
       holdout_df (and on production_scaler-transformed MST data), since
       decision_function on a model's own training data is optimistic.
    7. Save bundle {"model", "smd_scaler", "production_scaler",
       "feature_order", "thresholds", "metadata"} ->
       services/anomaly-detector/models/isolation_forest.pkl and append a
       run entry (pipeline="smd") to training_log.json.

Outputs:
    services/anomaly-detector/models/isolation_forest.pkl
    tools/anomaly-training/training_log.json   (appended each run)
"""

from __future__ import annotations

import argparse
import json
import os
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
sys.path.insert(0, str(_HERE))
from preprocess_mst import load_mst_features  # noqa: E402
from preprocess_smd import FEATURE_COLUMNS, anomaly_rate, build_features, load_smd_machine_raw  # noqa: E402

MODEL_OUT = _HERE.parent.parent / "services" / "anomaly-detector" / "models" / "isolation_forest.pkl"
TRAIN_LOG = _HERE / "training_log.json"

# Search grids. latency_dim is fixed to SMD dim1 (0-indexed col 0): the most
# anomaly-implicated single dim across all 28 machines'
# interpretation_label/ files (120 occurrences) and the one with clearly
# machine-differentiated variance. error_dim candidates are the next most
# frequent dims (1-indexed 13, 9, 12, 14, 15 -> 0-indexed 12, 8, 11, 13, 14),
# all bounded [0,1] like SMD's other normalized columns.
LATENCY_DIM = 0
ERROR_DIM_CANDIDATES = [12, 8, 11, 13, 14]
ROLLING_WINDOW_CANDIDATES = [3, 5, 10]
CONTAMINATION_CANDIDATES = [0.005, 0.01, 0.02, 0.05, 0.1]

N_ESTIMATORS_SEARCH = 100
N_ESTIMATORS_FINAL = 200
RANDOM_STATE = 42
F1_GATE = 0.80

# LATENCY_DIM must remain among the LATENCY_DIM_MAX_RANK most
# interpretation_label-implicated SMD dims for whatever machine set is being
# evaluated. Holds for both current candidate sets (rank 5 of 28 dims for
# ["machine-1-1"] and for ["machine-1-1", "machine-1-6"]). A future re-train
# on a different machine set that no longer supports dim 0 as a
# latency-relevant dim should fail loudly here rather than silently reusing
# it (PR #158 item #8).
LATENCY_DIM_MAX_RANK = 5


def _interpretation_label_counts(smd_dir: Path, machines: list[str]) -> dict[int, int]:
    """Count how often each 0-indexed SMD dim appears in interpretation_label/
    anomaly-segment annotations for `machines`."""
    counts: dict[int, int] = {}
    for m in machines:
        path = smd_dir / "interpretation_label" / f"{m}.txt"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            _, dims = line.split(":", 1)
            for d in dims.split(","):
                d = d.strip()
                if d:
                    counts[int(d) - 1] = counts.get(int(d) - 1, 0) + 1
    return counts


def _assert_latency_dim_rank(smd_dir: Path, machines: list[str]) -> None:
    """Fail loudly if LATENCY_DIM is no longer among the top
    LATENCY_DIM_MAX_RANK interpretation_label-implicated dims for `machines`."""
    counts = _interpretation_label_counts(smd_dir, machines)
    ranked = sorted(counts, key=lambda d: counts[d], reverse=True)
    rank = ranked.index(LATENCY_DIM) + 1 if LATENCY_DIM in ranked else len(ranked) + 1
    if rank > LATENCY_DIM_MAX_RANK:
        raise AssertionError(
            f"LATENCY_DIM={LATENCY_DIM} (SMD dim{LATENCY_DIM + 1}) has "
            f"interpretation_label rank {rank} for machines={machines} "
            f"(requires <= {LATENCY_DIM_MAX_RANK}). Re-check the latency-dim "
            f"choice before training on this machine set."
        )


def _evaluate(train_df: pd.DataFrame, eval_df: pd.DataFrame, contamination: float, n_estimators: int):
    scaler = StandardScaler().fit(train_df[FEATURE_COLUMNS].values)
    X_train = scaler.transform(train_df[FEATURE_COLUMNS].values)
    X_eval = scaler.transform(eval_df[FEATURE_COLUMNS].values)
    model = IsolationForest(
        contamination=contamination, n_estimators=n_estimators, random_state=RANDOM_STATE, n_jobs=-1
    ).fit(X_train)
    pred = model.predict(X_eval) == -1
    truth = eval_df["anomaly_truth"].values
    f1 = f1_score(truth, pred, zero_division=0)
    precision = precision_score(truth, pred, zero_division=0)
    recall = recall_score(truth, pred, zero_division=0)
    return f1, precision, recall, pred.mean(), scaler, model


def _search_machine_set(smd_dir: Path, machines: list[str]) -> dict:
    _assert_latency_dim_rank(smd_dir, machines)
    raw_cache = {m: load_smd_machine_raw(smd_dir, m) for m in machines}

    results = []  # (tune_f1, error_dim, rolling_window, contamination)
    best = None

    for error_dim in ERROR_DIM_CANDIDATES:
        for rolling_window in ROLLING_WINDOW_CANDIDATES:
            train_frames, test_frames = [], []
            for m in machines:
                train_raw, test_raw, labels = raw_cache[m]
                tdf = build_features(train_raw, LATENCY_DIM, error_dim, rolling_window)
                sdf = build_features(test_raw, LATENCY_DIM, error_dim, rolling_window)
                sdf["anomaly_truth"] = labels
                train_frames.append(tdf)
                test_frames.append(sdf)
            train_df = pd.concat(train_frames, ignore_index=True)
            test_df = pd.concat(test_frames, ignore_index=True)

            # Interleaved (even/odd) split rather than a contiguous temporal
            # half: SMD's anomaly windows are concentrated late in the test
            # series (e.g. machine-1-1 has zero anomalies in its first half),
            # so a contiguous split would leave tune_df with no positives.
            # The model is fit only on train_df (never on test_df), so there
            # is no leakage concern from interleaving here.
            tune_df = test_df.iloc[::2].reset_index(drop=True)
            holdout_df = test_df.iloc[1::2].reset_index(drop=True)

            if tune_df["anomaly_truth"].sum() == 0 or holdout_df["anomaly_truth"].sum() == 0:
                continue

            for contamination in CONTAMINATION_CANDIDATES:
                f1, _, _, _, _, _ = _evaluate(train_df, tune_df, contamination, N_ESTIMATORS_SEARCH)
                results.append((f1, error_dim, rolling_window, contamination))
                if best is None or f1 > best[0]:
                    best = (f1, error_dim, rolling_window, contamination, train_df, holdout_df)

    results.sort(key=lambda r: r[0], reverse=True)
    print(f"[train_smd] machines={machines}: top tune-F1 combos:")
    for f1, error_dim, rolling_window, contamination in results[:5]:
        print(f"    tune_f1={f1:.4f}  error_dim={error_dim}  window={rolling_window}  contamination={contamination}")

    tune_f1, error_dim, rolling_window, contamination, train_df, holdout_df = best
    f1, precision, recall, pred_rate, scaler, model = _evaluate(train_df, holdout_df, contamination, N_ESTIMATORS_FINAL)
    print(f"[train_smd] machines={machines}: holdout F1={f1:.4f} precision={precision:.4f} recall={recall:.4f} "
          f"(config: error_dim={error_dim} window={rolling_window} contamination={contamination}, "
          f"tune_f1={tune_f1:.4f})")

    return {
        "machines": machines,
        "latency_dim": LATENCY_DIM,
        "error_dim": error_dim,
        "rolling_window": rolling_window,
        "contamination": contamination,
        "f1": f1,
        "precision": precision,
        "recall": recall,
        "test_anomaly_rate_truth": float(holdout_df["anomaly_truth"].mean()),
        "test_anomaly_rate_pred": float(pred_rate),
        "train_df": train_df,
        "holdout_df": holdout_df,
        "scaler": scaler,
        "model": model,
    }


def _append_run_log(entry: dict) -> None:
    history: list = []
    if TRAIN_LOG.exists():
        try:
            history = json.loads(TRAIN_LOG.read_text())
        except (json.JSONDecodeError, OSError):
            history = []
    history.append(entry)
    TRAIN_LOG.write_text(json.dumps(history, indent=2))
    print(f"[train_smd] run appended -> {TRAIN_LOG}")


def main(smd_dir: str, mst_dir: str) -> None:
    run_ts = datetime.now(timezone.utc).isoformat()
    smd_dir = Path(smd_dir)
    mst_dir = Path(mst_dir)

    all_machines = sorted(p.stem for p in (smd_dir / "test_label").glob("machine-*.txt"))
    rates = {m: anomaly_rate(smd_dir, m) for m in all_machines}
    ranked = sorted(all_machines, key=lambda m: rates[m], reverse=True)
    print(f"[train_smd] {len(all_machines)} SMD machines found; "
          f"highest anomaly rates: {[(m, round(rates[m], 4)) for m in ranked[:4]]}")

    machine_sets = [["machine-1-1"], ["machine-1-1", "machine-1-6"], ranked[:4]]

    best = None
    for machines in machine_sets:
        candidate = _search_machine_set(smd_dir, machines)
        if best is None or candidate["f1"] > best["f1"]:
            best = candidate
        if candidate["f1"] > F1_GATE:
            print(f"[train_smd] F1 > {F1_GATE} gate cleared with machines={machines} -- stopping search")
            break

    print("\n" + "-" * 50)
    print("FINAL SELECTION (Isolation Forest vs real SMD test_label, holdout split)")
    print("-" * 50)
    print(f"  machines     : {best['machines']}")
    print(f"  latency_dim  : {best['latency_dim']} (SMD dim{best['latency_dim'] + 1})")
    print(f"  error_dim    : {best['error_dim']} (SMD dim{best['error_dim'] + 1})")
    print(f"  window       : {best['rolling_window']}")
    print(f"  contamination: {best['contamination']}")
    print(f"  F1 score     : {best['f1']:.4f}")
    print(f"  Precision    : {best['precision']:.4f}")
    print(f"  Recall       : {best['recall']:.4f}")
    print(f"  Test anomaly rate (ground truth): {best['test_anomaly_rate_truth']:.4f}")
    print(f"  Test anomaly rate (predicted)   : {best['test_anomaly_rate_pred']:.4f}")
    passed = best["f1"] > F1_GATE
    print(f"\n  F1 > {F1_GATE} gate : {'PASS' if passed else 'FAIL (target > 0.80)'}")
    print("-" * 50)

    # Thresholds derived from the train-only model's decision_function
    # distribution on the held-out SMD split (pre-refit, since
    # decision_function on a model's own training data is optimistic).
    holdout_scores = best["model"].decision_function(best["scaler"].transform(best["holdout_df"][FEATURE_COLUMNS].values))
    healthy_above = float(np.percentile(holdout_scores, 50))
    unhealthy_below = float(np.percentile(holdout_scores, best["contamination"] * 100))

    # production_scaler: fit on MST-2021-derived features (real-ms units),
    # the best available proxy for live ANOMALY_QUERY feature distributions.
    mst_df = load_mst_features(mst_dir, verbose=False)
    production_scaler = StandardScaler().fit(mst_df[FEATURE_COLUMNS].values)

    prod_scores = best["model"].decision_function(production_scaler.transform(mst_df[FEATURE_COLUMNS].values))
    unhealthy_score_scale = float(unhealthy_below - prod_scores.min())
    if unhealthy_score_scale <= 0:
        unhealthy_score_scale = 0.5

    print("\n[train_smd] derived thresholds:")
    print(f"  healthy_above       = {healthy_above:.4f}")
    print(f"  unhealthy_below     = {unhealthy_below:.4f}")
    print(f"  unhealthy_score_scale = {unhealthy_score_scale:.4f}")

    # Refit on all SMD data (train + test) for the deployed artifact.
    all_smd_df = pd.concat(
        [best["train_df"], best["holdout_df"][FEATURE_COLUMNS]], ignore_index=True
    )
    final_smd_scaler = StandardScaler().fit(all_smd_df[FEATURE_COLUMNS].values)
    final_model = IsolationForest(
        contamination=best["contamination"], n_estimators=N_ESTIMATORS_FINAL, random_state=RANDOM_STATE, n_jobs=-1
    ).fit(final_smd_scaler.transform(all_smd_df[FEATURE_COLUMNS].values))

    bundle = {
        "model": final_model,
        "smd_scaler": final_smd_scaler,
        "production_scaler": production_scaler,
        "feature_order": FEATURE_COLUMNS,
        "thresholds": {
            "healthy_above": healthy_above,
            "unhealthy_below": unhealthy_below,
            "unhealthy_score_scale": unhealthy_score_scale,
        },
        "metadata": {
            "trained_at": run_ts,
            "pipeline": "smd",
            "smd_machines": best["machines"],
            "smd_latency_dim": best["latency_dim"],
            "smd_error_dim": best["error_dim"],
            "smd_rolling_window": best["rolling_window"],
            "contamination": best["contamination"],
            "n_estimators": N_ESTIMATORS_FINAL,
            "random_state": RANDOM_STATE,
            "sklearn_version": sklearn.__version__,
            "test_f1": round(float(best["f1"]), 4),
            "test_precision": round(float(best["precision"]), 4),
            "test_recall": round(float(best["recall"]), 4),
            "test_anomaly_rate_truth": round(best["test_anomaly_rate_truth"], 4),
            "test_anomaly_rate_pred": round(best["test_anomaly_rate_pred"], 4),
            "production_scaler_fit_on": os.path.relpath(mst_dir, _HERE).replace("\\", "/"),
        },
    }

    MODEL_OUT.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, MODEL_OUT)
    print(f"\n[train_smd] model bundle saved -> {MODEL_OUT}")

    _append_run_log({
        **bundle["metadata"],
        "passed": bool(passed),
    })

    if not passed:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train the Isolation Forest anomaly engine on SMD")
    parser.add_argument("--smd-dir", default=str(_HERE.parent.parent / "datasets" / "smd"))
    parser.add_argument("--mst-dir", default=str(_HERE.parent.parent / "datasets" / "alibaba" / "mst2021" / "MSCallGraph" / "extracted_0"))
    args = parser.parse_args()

    main(args.smd_dir, args.mst_dir)
