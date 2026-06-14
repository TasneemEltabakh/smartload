"""
tools/anomaly-training/train_trend.py
─────────────────────────────────────
Quantile-calibrated training of the *temporal* anomaly engine (trend_forest).

Why this exists
---------------
The shipped Isolation Forest (tools/anomaly-training/retrain_calibrated.py) sees
only the four point features of a single window in isolation. It cannot, by
construction, distinguish a backend that is *steadily* slow from one that is
*slowly degrading* — the within-window shape (max/mean, std/mean) is identical,
only the absolute level rises relative to the backend's own normal. With no
memory of that normal, the point-feature model scores gradual degradation at
~0 recall.

This pipeline is the temporal analogue of retrain_calibrated.py. It keeps the
same structure — synthetic production-shaped windows, an IsolationForest fit on a
healthy operating region, two thresholds placed by QUANTILES of decision_function
over a held-out calibration set, acceptance gates, the same bundle schema — but
the feature space is the ENRICHED vector: the four point features plus the six
backend-relative temporal signals derived by services/anomaly-detector/features/
trend.py (mean_dev, max_dev, cusum_pos, slope, max_ratio, std_ratio). Those
signals carry the per-backend history that lets the model see a slow ramp.

Feature space (must match engines/trend_forest/engine.py ENRICHED_FEATURE_ORDER):
    (latency_ms[=window max], latency_rolling_mean_ms[=avg], error_rate,
     latency_rolling_std_ms[=std],
     mean_dev, max_dev, cusum_pos, slope, max_ratio, std_ratio)

Data
----
Windows come from the production-shaped benchmark generator
(experiments/anomaly-detection-bench/generators.py), driven through a FRESH
TrendExtractor per trace so the temporal signals are exactly what the live engine
would compute. The model is fit on the clean (label==0, post-warmup) portion of
ALL profiles — the clean-control trace plus the clean pre/post regions of the
injected profiles. Thresholds are calibrated and evaluated on seed ranges that
are DISJOINT from every other consumer of this generator:

    fit            seeds 700..739
    calibration    seeds 800..819   (threshold quantiles tuned here)
    evaluation     seeds 820..839   (held-out F1/precision/recall)

The benchmark evaluates on seeds 1..8 and the rule engine calibrates on
seeds 300..331; NONE of those appear here, so there is no leakage.

Run under scikit-learn 1.3.2 so the artifact matches the container pin and the
engine loads it warning-free.

    .venv/bin/python tools/anomaly-training/train_trend.py
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

# ── make the service root and the benchmark dir importable ───────────────────
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[2]
_SERVICE_ROOT = _REPO_ROOT / "services" / "anomaly-detector"
_BENCH_DIR = _REPO_ROOT / "experiments" / "anomaly-detection-bench"
for _p in (str(_SERVICE_ROOT), str(_BENCH_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from features.trend import ENRICHED_FEATURE_ORDER, TrendExtractor  # noqa: E402

import generators  # noqa: E402
from generators import GenParams  # noqa: E402

# ── fixed hyper-parameters ───────────────────────────────────────────────────
RANDOM_STATE = 42
N_ESTIMATORS = 200
# Contamination only sets where IsolationForest centres its decision_function;
# the operating verdict is set by the calibrated quantiles below, so the result
# is insensitive to this value within a sane range.
CONTAMINATION = 0.04

# Seed ranges — all disjoint from the benchmark (1..8) and the rule calibration
# (300..331). Inclusive lower bound, exclusive upper.
FIT_SEEDS = range(700, 740)
CALIBRATION_SEEDS = range(800, 820)
EVALUATION_SEEDS = range(820, 840)

# Threshold-quantile search grid. healthy_above / unhealthy_below are placed at
# percentiles of the CLEAN calibration decision_function scores; the pair that
# maximises binary F1 (status != "healthy") on the calibration set — subject to
# a clean false-positive-rate <= MAX_CLEAN_FPR constraint — is chosen.
HA_GRID = (10.0, 15.0, 20.0, 25.0)
UB_GRID = (2.0, 4.0, 6.0)
MAX_CLEAN_FPR = 0.05


def _enriched_dataset(seeds, params: GenParams):
    """Drive every (seed, profile) trace through a FRESH TrendExtractor and
    collect (enriched_vector, label, profile, warming_up) for each emitted
    window. The extractor is reset per trace so its state mirrors an independent
    backend coming online — exactly what the live engine sees via reset()."""
    extractor = TrendExtractor()
    vectors: list[list[float]] = []
    labels: list[int] = []
    profiles: list[str] = []
    warming: list[bool] = []
    for seed in seeds:
        for profile in generators.TRAIN_PROFILES:
            extractor.reset()
            for step in generators.generate(profile, seed, params):
                tf = extractor.update(
                    "b", step.latency_ms, step.latency_rolling_mean_ms,
                    step.error_rate, step.latency_rolling_std_ms,
                )
                vec = [
                    step.latency_ms,
                    step.latency_rolling_mean_ms,
                    step.error_rate,
                    step.latency_rolling_std_ms,
                    tf.mean_dev, tf.max_dev, tf.cusum_pos, tf.slope,
                    tf.max_ratio, tf.std_ratio,
                ]
                vectors.append(vec)
                labels.append(int(step.label))
                profiles.append(profile)
                warming.append(bool(tf.warming_up))
    return (
        np.asarray(vectors, dtype="float64"),
        np.asarray(labels, dtype=int),
        np.asarray(profiles, dtype=object),
        np.asarray(warming, dtype=bool),
    )


def _binary_f1(truth: np.ndarray, pred_nonhealthy: np.ndarray):
    tp = int(np.sum(pred_nonhealthy & truth))
    fp = int(np.sum(pred_nonhealthy & ~truth))
    fn = int(np.sum(~pred_nonhealthy & truth))
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return f1, precision, recall


def _verdict_nonhealthy(scores: np.ndarray, healthy_above: float) -> np.ndarray:
    """A window is non-healthy (degraded or unhealthy) iff its decision_function
    score is at or below healthy_above — the same boundary engine.score() uses to
    leave the 'healthy' tier."""
    return scores <= healthy_above


def main(out_path: str) -> None:
    params = GenParams()

    # ── 1. fit set: clean operating region across all profiles ───────────────
    fit_X, fit_y, _fit_prof, fit_warm = _enriched_dataset(FIT_SEEDS, params)
    healthy_mask = (fit_y == 0) & (~fit_warm)
    healthy = fit_X[healthy_mask]
    if len(healthy) < 100:
        raise SystemExit(f"fit set too small: {len(healthy)} healthy windows")

    scaler = StandardScaler().fit(healthy)
    model = IsolationForest(
        n_estimators=N_ESTIMATORS, contamination=CONTAMINATION,
        random_state=RANDOM_STATE, n_jobs=-1,
    ).fit(scaler.transform(healthy))

    # ── 2. calibration set: place + tune thresholds ──────────────────────────
    cal_X, cal_y, cal_prof, cal_warm = _enriched_dataset(CALIBRATION_SEEDS, params)
    cal_scores = model.decision_function(scaler.transform(cal_X))
    # Clean reference distribution = label-0, post-warmup windows. The quantiles
    # are read from this so the healthy boundary tracks real clean traffic.
    clean_ref_mask = (cal_y == 0) & (~cal_warm)
    clean_scores = cal_scores[clean_ref_mask]
    cal_truth = cal_y.astype(bool)
    # Genuine specificity control: the clean-control profile injects nothing, so
    # its post-warmup windows are all truly healthy traffic. Its false-positive
    # rate (fraction flagged non-healthy) is the operational FP-rate the
    # constraint and the benchmark care about — NOT the percentile-by-
    # construction fraction of the wider clean reference, which is locked to
    # ~HA_QUANTILE/100 because healthy_above IS the HA-th percentile of it.
    cc_mask = (cal_prof == "clean-control") & (~cal_warm)
    cc_scores = cal_scores[cc_mask]

    best = None  # (f1, precision, recall, ha_q, ub_q, healthy_above, unhealthy_below, fpr)
    for ha_q in HA_GRID:
        for ub_q in UB_GRID:
            if ub_q >= ha_q:
                continue
            healthy_above = float(np.percentile(clean_scores, ha_q))
            unhealthy_below = float(np.percentile(clean_scores, ub_q))
            if not (unhealthy_below < healthy_above):
                continue
            pred = _verdict_nonhealthy(cal_scores, healthy_above)
            # Operational clean false-positive rate, on the clean-control control.
            cc_pred = _verdict_nonhealthy(cc_scores, healthy_above)
            fpr = float(np.mean(cc_pred)) if len(cc_pred) else 0.0
            if fpr > MAX_CLEAN_FPR:
                continue
            f1, prec, rec = _binary_f1(cal_truth, pred)
            cand = (f1, prec, rec, ha_q, ub_q, healthy_above, unhealthy_below, fpr)
            if best is None or cand[0] > best[0]:
                best = cand

    if best is None:
        raise SystemExit(
            "calibration failed: no (HA, UB) pair satisfied the FP-rate constraint"
        )
    cal_f1, cal_prec, cal_rec, HA_QUANTILE, UB_QUANTILE, healthy_above, unhealthy_below, cal_fpr = best

    # ── 3. unhealthy_score_scale: span to the most anomalous calibration score ─
    anom_scores = cal_scores[cal_y == 1]
    most_anom = float(anom_scores.min()) if len(anom_scores) else unhealthy_below
    unhealthy_score_scale = max(0.05, float(unhealthy_below - most_anom))

    band = healthy_above - unhealthy_below

    # ── 4. held-out evaluation ───────────────────────────────────────────────
    ev_X, ev_y, ev_prof, _ev_warm = _enriched_dataset(EVALUATION_SEEDS, params)
    ev_scores = model.decision_function(scaler.transform(ev_X))
    ev_pred = _verdict_nonhealthy(ev_scores, healthy_above)
    ev_truth = ev_y.astype(bool)
    test_f1, test_precision, test_recall = _binary_f1(ev_truth, ev_pred)

    # Per-profile held-out F1 table (binary status != healthy). For the
    # clean-control profile (all label-0) F1/recall are undefined, so report its
    # false-positive rate instead.
    print(f"[train_trend] sklearn={sklearn.__version__} numpy={np.__version__}")
    print(f"[train_trend] contamination={CONTAMINATION} n_estimators={N_ESTIMATORS}")
    print(f"[train_trend] chosen HA_QUANTILE={HA_QUANTILE} UB_QUANTILE={UB_QUANTILE}")
    print(f"[train_trend] healthy_above={healthy_above:.4f} unhealthy_below={unhealthy_below:.4f} "
          f"band={band:.4f} unhealthy_score_scale={unhealthy_score_scale:.4f}")
    print(f"[train_trend] calibration: F1={cal_f1:.4f} precision={cal_prec:.4f} "
          f"recall={cal_rec:.4f} clean_FPR={cal_fpr:.4f}")
    print(f"[train_trend] held-out eval (status!=healthy): F1={test_f1:.4f} "
          f"precision={test_precision:.4f} recall={test_recall:.4f}")

    print("[train_trend] per-profile held-out (status!=healthy):")
    print("    profile               | F1      | precision | recall  | FP-rate")
    print("    ----------------------+---------+-----------+---------+--------")
    per_profile = {}
    for profile in generators.PROFILES:
        pmask = ev_prof == profile
        p_truth = ev_truth[pmask]
        p_pred = ev_pred[pmask]
        if p_truth.any():
            pf1, pprec, prec_recall = _binary_f1(p_truth, p_pred)
            fpr = float(np.mean(p_pred[~p_truth])) if (~p_truth).any() else 0.0
            per_profile[profile] = {"f1": pf1, "precision": pprec, "recall": prec_recall, "fp_rate": fpr}
            print(f"    {profile:<21} | {pf1:6.4f}  | {pprec:8.4f}  | {prec_recall:6.4f}  | {fpr:6.4f}")
        else:
            fpr = float(np.mean(p_pred)) if len(p_pred) else 0.0
            per_profile[profile] = {"f1": None, "precision": None, "recall": None, "fp_rate": fpr}
            print(f"    {profile:<21} | (clean) |   (clean) | (clean) | {fpr:6.4f}")

    # ── 5. acceptance gates ──────────────────────────────────────────────────
    # Degraded tier reachable on calibration: at least one calibration window
    # falls in the (unhealthy_below, healthy_above] band.
    degraded_reachable = bool(
        np.any((cal_scores <= healthy_above) & (cal_scores >= unhealthy_below))
    )
    grad = per_profile.get("gradual-degradation", {})
    grad_recall = grad.get("recall") or 0.0
    clean = per_profile.get("clean-control", {})
    clean_fp = clean.get("fp_rate") or 0.0

    ok = (
        band > 0.01
        and degraded_reachable
        and grad_recall > 0.3
        and clean_fp <= 0.06
    )
    print(f"[train_trend] GATES: band>0.01 ({band:.4f}) & degraded-reachable ({degraded_reachable}) "
          f"& gradual-recall>0.3 ({grad_recall:.4f}) & clean-FP<=0.06 ({clean_fp:.4f}) "
          f"=> {'PASS' if ok else 'FAIL'}")
    if not ok:
        raise SystemExit("acceptance gates not met — not writing bundle")

    # ── 6. bundle ────────────────────────────────────────────────────────────
    bundle = {
        "model": model,
        # In the enriched feature space the scaler IS the production scaler; the
        # engine only reads `production_scaler`. smd_scaler kept for
        # bundle-format compatibility with the point-feature engines.
        "smd_scaler": scaler,
        "production_scaler": scaler,
        "feature_order": list(ENRICHED_FEATURE_ORDER),
        "thresholds": {
            "healthy_above": healthy_above,
            "unhealthy_below": unhealthy_below,
            "unhealthy_score_scale": unhealthy_score_scale,
        },
        "metadata": {
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "pipeline": "trend_enriched_quantile_calibrated",
            "n_estimators": N_ESTIMATORS,
            "contamination": CONTAMINATION,
            "random_state": RANDOM_STATE,
            "fit_seeds": [FIT_SEEDS.start, FIT_SEEDS.stop],
            "calibration_seeds": [CALIBRATION_SEEDS.start, CALIBRATION_SEEDS.stop],
            "evaluation_seeds": [EVALUATION_SEEDS.start, EVALUATION_SEEDS.stop],
            "ha_quantile": HA_QUANTILE,
            "ub_quantile": UB_QUANTILE,
            "decision_band_width": round(band, 6),
            "sklearn_version": sklearn.__version__,
            "numpy_version": np.__version__,
            "test_f1": round(float(test_f1), 4),
            "test_precision": round(float(test_precision), 4),
            "test_recall": round(float(test_recall), 4),
            "feature_order": list(ENRICHED_FEATURE_ORDER),
            "note": "Enriched temporal-feature IsolationForest, quantile-calibrated. The four "
                    "point features are augmented with the six backend-relative trend signals "
                    "(mean_dev, max_dev, cusum_pos, slope, max_ratio, std_ratio) from "
                    "features/trend.py, giving the per-backend history a point-feature model "
                    "lacks — which is what lifts gradual-degradation recall off the floor. "
                    "Fit/calibration/evaluation seeds are disjoint from the benchmark "
                    "evaluation seeds (1..8) and the rule-engine calibration seeds (300..331), "
                    "so there is no evaluation leakage.",
        },
    }
    joblib.dump(bundle, out_path)
    print(f"[train_trend] bundle saved -> {out_path}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Quantile-calibrated training of the temporal (trend_forest) anomaly engine"
    )
    p.add_argument(
        "--out",
        default=str(_SERVICE_ROOT / "models" / "trend_forest.pkl"),
    )
    args = p.parse_args()
    main(args.out)
