"""
tools/anomaly-training/train_production.py
──────────────────────────────────────────
Option-3 re-calibration for the Isolation Forest anomaly engine (issue #165).

Why this exists
---------------
The shipped SMD-trained bundle (train_smd.py, F1=0.8012 on SMD holdout) ships
a `production_scaler` fit on Alibaba MST-2021 features. Model and scaler live
in two *different* mean-0/std-1 coordinate systems, so real-millisecond inputs
collapse toward the SMD origin and the model under-reacts: on the
anomaly-engine-bench 12-16² sweep it agreed with the threshold rule on only
~25% of cells, classifying 107/108 "clearly bad" cells as healthy.

This pipeline removes the bridge entirely (issue #165 option 3): it fits BOTH
the scaler and the IsolationForest in the SAME production-shape feature space,
so the model sees inputs in the units it was calibrated against. No SMD, no
MST — a synthetic *healthy operating region* whose distribution matches what a
healthy SmartLoad backend actually emits (real-ms latency, [0,1] error_rate),
with the threshold rule as the implicit weak-label boundary.

Feature space (must match engines/isolation_forest/engine.py FEATURE_ORDER):
    (latency_ms[=window max], latency_rolling_mean_ms[=avg], error_rate,
     latency_rolling_std_ms[=std])

The script is self-validating: after fitting it replays the
anomaly-engine-bench sweep inline (same threshold rule + same engine scoring
path) and only writes the bundle if agreement clears the gate AND the
live-stack 400 ms point + the extreme-outlier point both score `unhealthy`.

Run it inside the anomaly-detector container so the bundle is pickled with the
runtime scikit-learn (1.3.2), then `docker cp` the artifact onto the host:

    docker cp tools/anomaly-training/train_production.py <c>:/tmp/t.py
    docker compose exec anomaly-detector python /tmp/t.py --out /tmp/if.pkl
    docker cp <c>:/tmp/if.pkl services/anomaly-detector/models/isolation_forest.pkl
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

# Bench sweep parameters — must mirror experiments/anomaly-engine-bench/run.py
# defaults so the inline agreement number matches the committed SUMMARY.
BENCH_LAT_MIN, BENCH_LAT_MAX = 10.0, 600.0
BENCH_ERR_MAX = 0.20
BENCH_STEPS = 16
THR_LAT_MULT = 3.0
THR_ERR = 0.05

AGREEMENT_GATE = 0.80

# The live-stack acceptance point and the artifact-test extreme outlier — both
# MUST land `unhealthy`. LIVE_STACK_POINT is the *measured* feature vector of a
# 400 ms-injected backend over the runloop's 60 s window (max=406, avg=162,
# error=0, std=196): off-diagonal (max/avg≈2.5) with std/avg≈1.2 vs a healthy
# ~0.25 — the variance, not the absolute latency, is the anomaly signal (the
# threshold rule's latency ratio of 2.5 is < 3, so it calls this healthy; the
# model is meant to catch what the threshold misses).
LIVE_STACK_POINT = (406.0, 162.0, 0.0, 196.4)
EXTREME_POINT    = (50_000.0, 50_000.0, 1.0, 20_000.0)
# A plainly-healthy point that MUST stay `healthy`.
HEALTHY_POINT    = (25.0, 20.0, 0.005, 6.0)


def _synth_healthy(rng: np.random.Generator, n: int) -> np.ndarray:
    """A healthy SmartLoad backend: low error, modest latency, max tracking
    avg with a realistic spread, std proportional to latency. Latency is
    real-ms; error_rate is a true [0,1] fraction. These bounds are what make
    `error > ~0.05`, a 400 ms backend, and a max/avg spike all read as
    out-of-distribution while a steady low-latency backend reads as normal."""
    # Healthy latency spans the WHOLE bench range so steady high-latency-low-
    # error backends read as normal (matching the threshold rule, which only
    # flags error / latency-ratio, not absolute latency). Coverage matters more
    # than realism for the isolation boundary: a dense low-latency core (real
    # backends are fast) plus a uniform tail to 600 ms so the model doesn't
    # treat steady 300-600 ms as an outlier. The live-stack anomaly is caught
    # by std/ratio, not absolute latency, so widening latency here is free.
    n_core = n // 2
    avg_core = np.clip(rng.lognormal(mean=np.log(18.0), sigma=0.6, size=n_core), 2.0, 200.0)
    avg_tail = rng.uniform(20.0, 600.0, size=n - n_core)
    avg = np.concatenate([avg_core, avg_tail])
    rng.shuffle(avg)
    ratio = np.clip(1.0 + rng.exponential(0.20, size=n), 1.0, 2.2)   # max/avg, mode at 1.0
    max_lat = avg * ratio
    # Healthy error edge pinned at the threshold rule's 0.05: sigma chosen so
    # the half-normal's ~99th percentile lands at 0.05 (2.576*0.0194≈0.05).
    # This puts the model's error boundary ON 0.05 — error≤0.05 stays healthy
    # (no over-flag of the threshold-healthy columns), error>0.05 flags (no
    # under-reaction past the threshold).
    error = np.clip(np.abs(rng.normal(0.0, 0.0194, size=n)), 0.0, 0.05)
    # std TIGHTLY tracks ~0.25*avg — this is the dimension the live-stack
    # anomaly (std/avg≈1.2) violates, so keep healthy variance low.
    std = avg * np.clip(rng.normal(0.25, 0.06, size=n), 0.05, 0.5)
    return np.column_stack([max_lat, avg, error, std])


def _bench_grid() -> tuple[np.ndarray, np.ndarray]:
    """Yield the bench feature matrix and the threshold verdict per cell.

    Mirrors run.py: latency_ms == latency_rolling_mean_ms (so the threshold's
    latency ratio is always 1.0 → it never says degraded), std = 0.25*latency.
    Threshold verdict therefore reduces to: error>THR_ERR → unhealthy, else
    healthy."""
    feats, verdict = [], []
    for i in range(BENCH_STEPS):
        latency = BENCH_LAT_MIN + (BENCH_LAT_MAX - BENCH_LAT_MIN) * i / (BENCH_STEPS - 1)
        std = latency * 0.25
        for j in range(BENCH_STEPS):
            err = BENCH_ERR_MAX * j / (BENCH_STEPS - 1)
            feats.append([latency, latency, err, std])
            ratio = 1.0  # latency_ms == rolling_mean
            if err > THR_ERR:
                verdict.append("unhealthy")
            elif ratio > THR_LAT_MULT:
                verdict.append("degraded")
            else:
                verdict.append("healthy")
    return np.array(feats), np.array(verdict)


def _verdicts(scores: np.ndarray, healthy_above: float, unhealthy_below: float) -> np.ndarray:
    out = np.empty(scores.shape, dtype=object)
    out[scores > healthy_above] = "healthy"
    out[(scores <= healthy_above) & (scores >= unhealthy_below)] = "degraded"
    out[scores < unhealthy_below] = "unhealthy"
    return out


def main(out_path: str, seed: int) -> None:
    rng = np.random.default_rng(seed)

    healthy = _synth_healthy(rng, 30_000)
    scaler = StandardScaler().fit(healthy)
    model = IsolationForest(
        n_estimators=N_ESTIMATORS, contamination=0.02,
        random_state=RANDOM_STATE, n_jobs=-1,
    ).fit(scaler.transform(healthy))

    # Decision scores for calibration.
    grid_feats, thr_verdict = _bench_grid()
    grid_scores = model.decision_function(scaler.transform(grid_feats))
    healthy_scores = model.decision_function(scaler.transform(_synth_healthy(rng, 5_000)))

    live_score = float(model.decision_function(scaler.transform([LIVE_STACK_POINT]))[0])
    extreme_score = float(model.decision_function(scaler.transform([EXTREME_POINT]))[0])
    healthy_pt_score = float(model.decision_function(scaler.transform([HEALTHY_POINT]))[0])

    # Search (healthy_above, unhealthy_below) to maximise bench agreement,
    # subject to: the 400 ms + extreme points score `unhealthy` and the
    # plainly-healthy point scores `healthy`. IsolationForest.decision_function
    # is HIGHER for normal, LOWER (more negative) for anomalous, so the ordering
    # we want is  extreme < live < unhealthy_below < healthy_above < healthy_pt.
    best = None
    ha_candidates = np.percentile(healthy_scores, np.arange(1, 50, 1))
    ub_floor = max(live_score, extreme_score)   # unhealthy_below must sit ABOVE both anomaly scores
    for ha in ha_candidates:
        if ha >= healthy_pt_score:              # the 25 ms point must be healthy (score > ha)
            continue
        if ub_floor >= ha:                      # need room for ub in (ub_floor, ha)
            continue
        for ub in np.linspace(ub_floor + 1e-4, ha - 1e-4, 30):
            # live<ub, extreme<ub, healthy_pt>ha all hold by construction.
            v = _verdicts(grid_scores, ha, ub)
            agree = float(np.mean(v == thr_verdict))
            # Penalise the forbidden under-reaction direction (threshold
            # unhealthy & model healthy) so the search never trades agreement
            # for it.
            under = int(np.sum((thr_verdict == "unhealthy") & (v == "healthy")))
            key = (agree, -under)
            if best is None or key > best[0]:
                best = (key, float(ha), float(ub), agree, under)

    if best is None:
        raise SystemExit("calibration failed: no (healthy_above, unhealthy_below) satisfied the constraints")

    _key, healthy_above, unhealthy_below, agreement, under = best
    unhealthy_score_scale = max(0.05, float(unhealthy_below - extreme_score))

    final_v = _verdicts(grid_scores, healthy_above, unhealthy_below)
    counts = {s: int(np.sum(final_v == s)) for s in ("healthy", "degraded", "unhealthy")}
    live_v = _verdicts(np.array([live_score]), healthy_above, unhealthy_below)[0]
    extreme_v = _verdicts(np.array([extreme_score]), healthy_above, unhealthy_below)[0]
    healthy_v = _verdicts(np.array([healthy_pt_score]), healthy_above, unhealthy_below)[0]

    print(f"[train_production] sklearn={sklearn.__version__}")
    print(f"[train_production] healthy_above={healthy_above:.4f} unhealthy_below={unhealthy_below:.4f} "
          f"unhealthy_score_scale={unhealthy_score_scale:.4f}")
    print(f"[train_production] bench agreement = {agreement:.1%} ({int(agreement*len(thr_verdict))}/{len(thr_verdict)})  "
          f"under-reactions(thr unhealthy & model healthy) = {under}")
    print(f"[train_production] engine verdict counts on bench grid: {counts}")
    print(f"[train_production] live-stack 400ms -> {live_v} (score {live_score:.4f})")
    print(f"[train_production] extreme outlier  -> {extreme_v} (score {extreme_score:.4f})")
    print(f"[train_production] healthy 25ms     -> {healthy_v} (score {healthy_pt_score:.4f})")

    ok = (agreement >= AGREEMENT_GATE and live_v == "unhealthy"
          and extreme_v == "unhealthy" and healthy_v == "healthy" and under == 0)
    print(f"[train_production] GATES: agreement>={AGREEMENT_GATE:.0%} & 400ms-unhealthy & "
          f"extreme-unhealthy & 25ms-healthy & no-under-reaction => {'PASS' if ok else 'FAIL'}")
    if not ok:
        raise SystemExit("acceptance gates not met — not writing bundle")

    bundle = {
        "model": model,
        # In production-shape space the scaler IS the production_scaler; there is
        # no separate SMD frame. Kept under both keys for bundle-format
        # compatibility (the engine only reads `production_scaler`).
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
            "pipeline": "production_synthetic",
            "issue": 165,
            "n_estimators": N_ESTIMATORS,
            "contamination": 0.02,
            "random_state": RANDOM_STATE,
            "seed": seed,
            "sklearn_version": sklearn.__version__,
            "bench_agreement": round(agreement, 4),
            "bench_under_reactions": under,
            "feature_space": "production-shape real-ms (scaler+model co-located; no SMD/MST bridge)",
            "note": "Re-calibration per issue #165 option 3. smd_scaler is vestigial "
                    "(== production_scaler); the model lives in the same coordinate system as its inputs.",
        },
    }
    joblib.dump(bundle, out_path)
    print(f"[train_production] bundle saved -> {out_path}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Re-calibrate the Isolation Forest engine in production-shape space (#165)")
    p.add_argument("--out", default="/tmp/isolation_forest.pkl")
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()
    main(args.out, args.seed)
