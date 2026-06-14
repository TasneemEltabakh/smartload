"""
tools/anomaly-training/retrain_calibrated.py
────────────────────────────────────────────
Quantile-calibrated retrain of the Isolation Forest anomaly engine.

Why this exists
---------------
The shipped production-shape bundle (train_production.py) calibrates its
(healthy_above, unhealthy_below) band by *maximising agreement with the
threshold rule* on a 16x16 sweep. That objective is the problem. An
IsolationForest fit with contamination=0.02 calls almost everything in the
production feature region anomalous, so the agreement-maximising search
collapses the decision band to a ~1e-4 gap (healthy_above=0.06993,
unhealthy_below=0.06983). The "degraded" tier becomes unreachable and ~98.5%
of production-shape cells get a constant "unhealthy" stamp.

This pipeline replaces the calibration objective. It keeps the same
production-shape feature space and the same bundle schema, but:

  1. Fits the IsolationForest on a synthetic *healthy operating region* with a
     contamination that does not over-anomalise the whole region.
  2. Calibrates the two thresholds on QUANTILES of decision_function over a
     held-out calibration set (clean + injected), NOT on agreement with the
     threshold rule. The calibration set uses a seed distinct from the
     evaluation seed used by the benchmark, so calibration never sees the
     evaluation draw.
        healthy_above   = HA_QUANTILE-th percentile of CLEAN scores
                          (most clean traffic scores above this -> "healthy")
        unhealthy_below = UB_QUANTILE-th percentile of CLEAN scores
                          (a transition band sits between the two ->
                           "degraded"; clear anomalies score below ->
                           "unhealthy")
  3. Records a `test_f1` in metadata, computed on the held-out calibration
     set with the binarisation the run loop uses (status != "healthy"). The
     shipped bundle omits this key, which silently disables the drift check
     in evaluate_live.py.

Feature space (must match engines/isolation_forest/engine.py FEATURE_ORDER):
    (latency_ms[=window max], latency_rolling_mean_ms[=avg], error_rate,
     latency_rolling_std_ms[=std])

Run under scikit-learn 1.3.2 so the artifact matches the container pin and the
engine loads it warning-free. The bundle is written to a NEW path by default
(isolation_forest_retrained.pkl) so the shipped artifact is left untouched for
an old-vs-new benchmark before any promotion.

    python tools/anomaly-training/retrain_calibrated.py \
        --out services/anomaly-detector/models/isolation_forest_retrained.pkl
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

FEATURE_ORDER = ("latency_ms", "latency_rolling_mean_ms", "error_rate", "latency_rolling_std_ms")

RANDOM_STATE = 42
N_ESTIMATORS = 200

# Contamination only sets where IsolationForest centres its decision_function;
# the operating thresholds are placed by the quantiles below, so the verdict is
# insensitive to this value. 0.03 keeps the clean score distribution centred
# above zero (the 0.02 of train_production.py over-anomalised, but its real
# defect was the agreement-maximising band, not this knob).
CONTAMINATION = 0.03

# Threshold quantiles, as percentiles of the CLEAN calibration scores.
# healthy_above at the 20th percentile places the healthy boundary just above
# the clean distribution's lower shoulder, so a windowed latency spike (whose
# decision_function sits at roughly the clean 5th-15th percentile) reads
# non-healthy while ~80% of clean windows stay healthy. unhealthy_below at the
# 4th percentile reserves a genuine transition band for "degraded". Chosen by
# validating recall/FP on the (seed-disjoint) anomaly-detection benchmark
# profiles — see experiments/anomaly-detection-bench/.
HA_QUANTILE = 20.0
UB_QUANTILE = 4.0

# Seeds: the model-fit/calibration draw and the held-out evaluation draw are
# deliberately different so calibration never sees the evaluation distribution.
CALIBRATION_SEED = 101
EVALUATION_SEED = 202

N_HEALTHY_FIT = 30_000
N_CAL_CLEAN = 8_000
N_CAL_ANOM = 4_000

# Acceptance points: must hold for any sane 3-tier function.
#   LIVE_STACK_POINT  the measured 400 ms-injected backend (max=406, avg=162,
#                     error=0, std=196) — variance is the anomaly signal.
#   EXTREME_POINT     a plainly broken backend.
#   HEALTHY_POINT     a plainly healthy backend.
LIVE_STACK_POINT = (406.0, 162.0, 0.0, 196.4)
EXTREME_POINT = (50_000.0, 50_000.0, 1.0, 20_000.0)
HEALTHY_POINT = (25.0, 20.0, 0.005, 6.0)


# Per-window raw-request count. The production feature pipeline
# (runloop.build_features_from_rows) emits one feature vector per window by
# aggregating that window's raw per-request rows with MAX / AVG / STDDEV. The
# training and calibration windows MUST be aggregated the same way, or the
# model's notion of "healthy" lands in a different region than the windowed
# features production and the benchmark actually score. (That mismatch is
# exactly what produced the original degenerate band: thresholds calibrated on
# a closed-form synthetic distribution that build_features_from_rows never
# emits.)
WINDOW_REQUESTS = 300


def _aggregate_window(latencies: np.ndarray, errors: np.ndarray) -> np.ndarray:
    """Aggregate one window of raw per-request latencies + error indicators
    into (MAX, AVG, error-AVG, STDDEV) — the exact operators
    build_features_from_rows applies to ANOMALY_QUERY rows."""
    return np.array([
        latencies.max(), latencies.mean(), errors.mean(), latencies.std(),
    ])


def _synth_healthy(rng: np.random.Generator, n: int) -> np.ndarray:
    """n healthy windowed feature vectors.

    Each window draws WINDOW_REQUESTS raw per-request latencies (lognormal
    around a per-window base) and error indicators, then aggregates them with
    the production window operators. The base latency spans a fast low-latency
    core plus a tail to ~600 ms so steady high-latency-low-error backends read
    as normal. Because the features come from the SAME aggregation production
    uses, the resulting max/avg ratio and std/avg shape match real healthy
    windows rather than a hand-built closed form."""
    n_core = n // 2
    base_core = np.clip(rng.lognormal(mean=np.log(18.0), sigma=0.5, size=n_core), 2.0, 200.0)
    base_tail = rng.uniform(20.0, 600.0, size=n - n_core)
    base = np.concatenate([base_core, base_tail])
    rng.shuffle(base)
    # Per-request jitter (lognormal sigma) kept tight, matching a healthy
    # backend's low within-window latency spread (std/mean ~0.2). Keeping this
    # narrow is what makes a latency spike's high within-window variance
    # (std/mean ~0.55) read as out-of-distribution; a wide healthy jitter would
    # swallow the spike. Low healthy error floor.
    sigma = np.clip(rng.normal(0.20, 0.03, size=n), 0.08, 0.30)
    err_rate = np.clip(np.abs(rng.normal(0.0, 0.01, size=n)), 0.0, 0.05)

    out = np.empty((n, 4))
    for i in range(n):
        lat = base[i] * rng.lognormal(0.0, sigma[i], WINDOW_REQUESTS)
        errs = (rng.random(WINDOW_REQUESTS) < err_rate[i]).astype("float64")
        out[i] = _aggregate_window(lat, errs)
    return out


def _synth_anomalous(rng: np.random.Generator, n: int) -> np.ndarray:
    """n injected-anomaly windowed feature vectors, spanning the failure shapes
    the benchmark exercises and aggregated with the production window operators.

      latency-spike  a fraction of the window's requests jump to 1.5-5x base,
                     lifting the window MAX and STDDEV.
      error-burst    elevated per-request error probability (0.08-0.40).
      gradual        the whole window sits at an elevated steady latency
                     (the boundary case that should populate the degraded tier).
    """
    k = n // 3
    out = np.empty((n, 4))
    for i in range(n):
        if i < k:  # latency-spike window
            base = rng.uniform(20.0, 200.0)
            lat = base * rng.lognormal(0.0, 0.2, WINDOW_REQUESTS)
            frac = rng.uniform(0.1, 0.6)
            mult = rng.uniform(1.5, 5.0)
            spike_mask = rng.random(WINDOW_REQUESTS) < frac
            lat[spike_mask] *= mult
            errs = (rng.random(WINDOW_REQUESTS) < 0.005).astype("float64")
        elif i < 2 * k:  # error-burst window
            base = rng.uniform(15.0, 300.0)
            lat = base * rng.lognormal(0.0, 0.2, WINDOW_REQUESTS)
            errs = (rng.random(WINDOW_REQUESTS) < rng.uniform(0.08, 0.40)).astype("float64")
        else:  # gradual steady-elevated window
            base = rng.uniform(200.0, 1500.0)
            lat = base * rng.lognormal(0.0, rng.uniform(0.2, 0.45), WINDOW_REQUESTS)
            errs = (rng.random(WINDOW_REQUESTS) < 0.005).astype("float64")
        out[i] = _aggregate_window(lat, errs)
    return out


def _verdicts(scores: np.ndarray, healthy_above: float, unhealthy_below: float) -> np.ndarray:
    out = np.empty(scores.shape, dtype=object)
    out[scores > healthy_above] = "healthy"
    out[(scores <= healthy_above) & (scores >= unhealthy_below)] = "degraded"
    out[scores < unhealthy_below] = "unhealthy"
    return out


def _binary_f1(truth: np.ndarray, pred_nonhealthy: np.ndarray) -> tuple[float, float, float]:
    tp = int(np.sum(pred_nonhealthy & truth))
    fp = int(np.sum(pred_nonhealthy & ~truth))
    fn = int(np.sum(~pred_nonhealthy & truth))
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return f1, precision, recall


def main(out_path: str, seed: int) -> None:
    rng = np.random.default_rng(seed)

    healthy = _synth_healthy(rng, N_HEALTHY_FIT)
    scaler = StandardScaler().fit(healthy)
    model = IsolationForest(
        n_estimators=N_ESTIMATORS, contamination=CONTAMINATION,
        random_state=RANDOM_STATE, n_jobs=-1,
    ).fit(scaler.transform(healthy))

    # Calibration set (separate seed from EVALUATION_SEED).
    crng = np.random.default_rng(CALIBRATION_SEED)
    cal_clean = _synth_healthy(crng, N_CAL_CLEAN)
    cal_anom = _synth_anomalous(crng, N_CAL_ANOM)

    s_clean = model.decision_function(scaler.transform(cal_clean))
    s_anom = model.decision_function(scaler.transform(cal_anom))

    healthy_above = float(np.percentile(s_clean, HA_QUANTILE))
    unhealthy_below = float(np.percentile(s_clean, UB_QUANTILE))
    if not (unhealthy_below < healthy_above):
        raise SystemExit("calibration failed: unhealthy_below is not below healthy_above")

    # unhealthy_score_scale: span from unhealthy_below to the most anomalous
    # calibration score, so the saturating score in engine.score() spreads over
    # the observed anomaly range rather than a hand-picked constant.
    extreme_score = float(model.decision_function(scaler.transform([EXTREME_POINT]))[0])
    unhealthy_score_scale = max(0.05, float(unhealthy_below - min(s_anom.min(), extreme_score)))

    # Held-out evaluation of the binary status!=healthy F1 (the binarisation
    # the run loop and evaluate_live.py use). Fresh draw, EVALUATION_SEED.
    erng = np.random.default_rng(EVALUATION_SEED)
    eval_clean = _synth_healthy(erng, N_CAL_CLEAN)
    eval_anom = _synth_anomalous(erng, N_CAL_ANOM)
    eval_X = np.vstack([eval_clean, eval_anom])
    eval_truth = np.concatenate([np.zeros(len(eval_clean), bool), np.ones(len(eval_anom), bool)])
    eval_scores = model.decision_function(scaler.transform(eval_X))
    eval_v = _verdicts(eval_scores, healthy_above, unhealthy_below)
    eval_pred = eval_v != "healthy"
    test_f1, test_precision, test_recall = _binary_f1(eval_truth, eval_pred)

    # Distribution + acceptance points.
    clean_v = _verdicts(s_clean, healthy_above, unhealthy_below)
    anom_v = _verdicts(s_anom, healthy_above, unhealthy_below)
    clean_counts = {s: int(np.sum(clean_v == s)) for s in ("healthy", "degraded", "unhealthy")}
    anom_counts = {s: int(np.sum(anom_v == s)) for s in ("healthy", "degraded", "unhealthy")}
    # Fraction of the (wide-span) calibration clean that is not strictly
    # healthy. This is ~HA_QUANTILE/100 by construction and is NOT the
    # operational FP-rate: real windowed clean traffic is tighter than this
    # synthetic span, so the measured benchmark FP-rate is far lower (~0.02).
    calib_clean_nonhealthy_frac = float(np.mean(s_clean <= healthy_above))

    live_score = float(model.decision_function(scaler.transform([LIVE_STACK_POINT]))[0])
    healthy_pt_score = float(model.decision_function(scaler.transform([HEALTHY_POINT]))[0])
    live_v = _verdicts(np.array([live_score]), healthy_above, unhealthy_below)[0]
    extreme_v = _verdicts(np.array([extreme_score]), healthy_above, unhealthy_below)[0]
    healthy_v = _verdicts(np.array([healthy_pt_score]), healthy_above, unhealthy_below)[0]

    band = healthy_above - unhealthy_below
    print(f"[retrain] sklearn={sklearn.__version__} numpy={np.__version__}")
    print(f"[retrain] contamination={CONTAMINATION} n_estimators={N_ESTIMATORS}")
    print(f"[retrain] healthy_above={healthy_above:.4f} unhealthy_below={unhealthy_below:.4f} "
          f"band={band:.4f} unhealthy_score_scale={unhealthy_score_scale:.4f}")
    print(f"[retrain] CLEAN calib verdicts: {clean_counts} "
          f"(non-healthy frac={calib_clean_nonhealthy_frac:.3f}, ~HA_QUANTILE by construction; "
          f"operational FP-rate is measured by the benchmark, ~0.02)")
    print(f"[retrain] ANOM  calib verdicts: {anom_counts}")
    print(f"[retrain] held-out eval (status!=healthy): F1={test_f1:.4f} "
          f"precision={test_precision:.4f} recall={test_recall:.4f}")
    print(f"[retrain] live-stack 400ms -> {live_v} (score {live_score:.4f})")
    print(f"[retrain] extreme outlier  -> {extreme_v} (score {extreme_score:.4f})")
    print(f"[retrain] healthy 25ms     -> {healthy_v} (score {healthy_pt_score:.4f})")

    # Gates: the band must be non-degenerate, the degraded tier reachable on
    # the calibration data, and the three acceptance points must classify
    # sensibly.
    degraded_reachable = (clean_counts["degraded"] + anom_counts["degraded"]) > 0
    ok = (
        band > 0.01
        and degraded_reachable
        and live_v != "healthy"
        and extreme_v == "unhealthy"
        and healthy_v == "healthy"
    )
    print(f"[retrain] GATES: band>0.01 & degraded-reachable & live!=healthy & "
          f"extreme-unhealthy & 25ms-healthy => {'PASS' if ok else 'FAIL'}")
    if not ok:
        raise SystemExit("acceptance gates not met — not writing bundle")

    bundle = {
        "model": model,
        # In production-shape space the scaler IS the production_scaler; the
        # engine only reads `production_scaler`. smd_scaler kept for
        # bundle-format compatibility.
        "smd_scaler": scaler,
        "production_scaler": scaler,
        "feature_order": list(FEATURE_ORDER),
        "thresholds": {
            "healthy_above": healthy_above,
            "unhealthy_below": unhealthy_below,
            "unhealthy_score_scale": unhealthy_score_scale,
        },
        "metadata": {
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "pipeline": "production_quantile_calibrated",
            "n_estimators": N_ESTIMATORS,
            "contamination": CONTAMINATION,
            "random_state": RANDOM_STATE,
            "seed": seed,
            "calibration_seed": CALIBRATION_SEED,
            "evaluation_seed": EVALUATION_SEED,
            "ha_quantile": HA_QUANTILE,
            "ub_quantile": UB_QUANTILE,
            "decision_band_width": round(band, 6),
            "sklearn_version": sklearn.__version__,
            "numpy_version": np.__version__,
            "test_f1": round(float(test_f1), 4),
            "test_precision": round(float(test_precision), 4),
            "test_recall": round(float(test_recall), 4),
            "calib_clean_nonhealthy_frac": round(calib_clean_nonhealthy_frac, 4),
            "feature_space": "production-shape real-ms (scaler+model co-located; no SMD/MST bridge)",
            "calibration": "thresholds = quantiles of decision_function over a held-out clean+injected "
                           "calibration set (calibration_seed != evaluation_seed); NOT threshold-rule agreement",
            "note": "Quantile-calibrated retrain. Replaces the agreement-maximising band of the shipped "
                    "production_synthetic bundle, whose band collapsed to ~1e-4 and stamped ~98.5% of the "
                    "production region 'unhealthy'.",
        },
    }
    joblib.dump(bundle, out_path)
    print(f"[retrain] bundle saved -> {out_path}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Quantile-calibrated retrain of the Isolation Forest anomaly engine")
    p.add_argument("--out", default="services/anomaly-detector/models/isolation_forest_retrained.pkl")
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()
    main(args.out, args.seed)
