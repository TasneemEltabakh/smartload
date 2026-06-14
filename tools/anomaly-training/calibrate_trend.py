"""
tools/anomaly-training/calibrate_trend.py
──────────────────────────────────────────
Calibrate the thresholds of the rule-based ``trend_rule`` engine on
production-shaped streams at seeds DISJOINT from the benchmark's evaluation
seeds, so the engine's defaults are never tuned on the data it is scored against.

What is calibrated
------------------
The benchmark's primary metrics binarise the three-tier status as
``status != "healthy"``. That boundary is set entirely by the *degraded-entry*
thresholds (the lower of each channel's two gates) plus the recovery-slope
suppressor — the degraded-vs-unhealthy split changes only the 3-tier confusion
matrix, not precision/recall/F1/FP-rate. So this script grid-searches exactly
those binary-relevant knobs:

    cusum_degraded, max_dev_degraded, mean_dev_degraded, recovery_slope

choosing the combination that maximises mean binary F1 across the four training
profiles on the calibration seeds, subject to a clean-traffic false-positive
constraint. The *unhealthy* thresholds (which only affect tiering/severity) are
then placed at quantiles of the anomalous-window signal so the unhealthy tier is
reserved for clearly-severe windows.

Seeds: calibration uses 300..331. The benchmark evaluates on 1..8 and the
trend_forest model fits on 700..839 — all disjoint, so there is no leakage.

    python tools/anomaly-training/calibrate_trend.py
"""

from __future__ import annotations

import json
import sys
from itertools import product
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[2]
_BENCH = _REPO / "experiments" / "anomaly-detection-bench"
_SVC = _REPO / "services" / "anomaly-detector"
for _p in (str(_BENCH), str(_SVC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import generators  # noqa: E402
from generators import GenParams  # noqa: E402
from engine_base import BackendFeatures  # noqa: E402
from engines.trend_rule.engine import TrendRuleEngine  # noqa: E402
from features.trend import TrendExtractor  # noqa: E402

CALIBRATION_SEEDS = range(300, 332)
FP_BUDGET = 0.05  # max tolerated clean-traffic false-positive rate

# Binary-relevant grid (degraded-entry gates + recovery suppressor).
GRID = {
    "cusum_degraded": [2.0, 3.0, 4.0, 5.0],
    "max_dev_degraded": [0.30, 0.40, 0.50],
    "mean_dev_degraded": [0.12, 0.15, 0.18, 0.22],
    "recovery_slope": [0.02, 0.03, 0.04],
}


def _feat(step):
    return BackendFeatures("b", step.latency_ms, step.latency_rolling_mean_ms,
                           step.error_rate, step.sample_count, step.latency_rolling_std_ms)


def _binary_scores(engine, profiles, seeds, params):
    """Per-profile (precision, recall, f1, fp_rate) averaged over seeds, plus the
    pooled clean false-positive rate across all profiles."""
    per_profile = {}
    pooled_fp = 0
    pooled_clean = 0
    for profile in profiles:
        rows = []
        for seed in seeds:
            engine.reset()
            steps = generators.generate(profile, seed, params)
            pred = [engine.score(_feat(s)).status != "healthy" for s in steps]
            truth = [bool(s.label) for s in steps]
            tp = sum(p and t for p, t in zip(pred, truth))
            fp = sum(p and not t for p, t in zip(pred, truth))
            fn = sum((not p) and t for p, t in zip(pred, truth))
            tn = sum((not p) and (not t) for p, t in zip(pred, truth))
            prec = tp / (tp + fp) if (tp + fp) else np.nan
            rec = tp / (tp + fn) if (tp + fn) else np.nan
            f1 = (2 * prec * rec / (prec + rec)
                  if prec and rec and prec == prec and rec == rec
                  else (0.0 if (tp + fp + fn) else np.nan))
            fpr = fp / (fp + tn) if (fp + tn) else np.nan
            rows.append((prec, rec, f1, fpr))
            pooled_fp += fp
            pooled_clean += fp + tn
        per_profile[profile] = np.nanmean(np.array(rows, float), axis=0)
    pooled_fp_rate = pooled_fp / pooled_clean if pooled_clean else np.nan
    return per_profile, pooled_fp_rate


def main() -> None:
    params = GenParams()
    profiles = generators.TRAIN_PROFILES
    seeds = list(CALIBRATION_SEEDS)

    keys = list(GRID)
    best = None
    for combo in product(*[GRID[k] for k in keys]):
        cfg = dict(zip(keys, combo))
        engine = TrendRuleEngine(
            cusum_degraded=cfg["cusum_degraded"],
            max_dev_degraded=cfg["max_dev_degraded"],
            mean_dev_degraded=cfg["mean_dev_degraded"],
            recovery_slope=cfg["recovery_slope"],
        )
        per_profile, pooled_fp = _binary_scores(engine, profiles, seeds, params)
        mean_f1 = float(np.nanmean([per_profile[p][2] for p in profiles]))
        # Mean recall on the injecting profiles (used only to break F1 ties).
        inj = [p for p in profiles if p not in generators.NON_INJECTING_PROFILES]
        mean_recall = float(np.nanmean([per_profile[p][1] for p in inj]))
        feasible = pooled_fp <= FP_BUDGET
        key = (feasible, round(mean_f1, 4), round(mean_recall, 4))
        if best is None or key > best[0]:
            best = (key, cfg, per_profile, pooled_fp, mean_f1, mean_recall)

    _, cfg, per_profile, pooled_fp, mean_f1, mean_recall = best
    print(f"[calibrate_trend] calibration seeds {seeds[0]}..{seeds[-1]} "
          f"on profiles {list(profiles)}")
    print(f"[calibrate_trend] best degraded-entry thresholds (max mean-F1 s.t. "
          f"pooled clean FP <= {FP_BUDGET}):")
    for k in keys:
        print(f"    {k:18s} = {cfg[k]}")
    print(f"[calibrate_trend] calibration mean-F1={mean_f1:.4f} "
          f"mean-recall(injecting)={mean_recall:.4f} pooled-clean-FP={pooled_fp:.4f}")
    for p in profiles:
        pr, rc, f1, fpr = per_profile[p]
        print(f"    {p:22s} P={pr:.3f} R={rc:.3f} F1={f1:.3f} FP={fpr:.3f}")

    # ── place the unhealthy (severity) thresholds from anomalous-window
    # quantiles, so the unhealthy tier is reserved for clearly-severe windows.
    ext = TrendExtractor()
    cu, md, sd = [], [], []  # cusum, max_dev, mean_dev over anomalous windows
    for profile in profiles:
        if profile in generators.NON_INJECTING_PROFILES:
            continue
        for seed in seeds:
            ext.reset()
            for step in generators.generate(profile, seed, params):
                tf = ext.update("b", step.latency_ms, step.latency_rolling_mean_ms,
                                step.error_rate, step.latency_rolling_std_ms)
                if step.label and not tf.warming_up:
                    cu.append(tf.cusum_pos); md.append(tf.max_dev); sd.append(tf.mean_dev)
    unhealthy = {
        "cusum_unhealthy": round(float(np.percentile(cu, 60)), 3),
        "max_dev_unhealthy": round(float(np.percentile(md, 60)), 3),
        "mean_dev_unhealthy": round(float(np.percentile(sd, 60)), 3),
    }
    print(f"[calibrate_trend] unhealthy (severity) thresholds @P60 of anomalous "
          f"windows: {unhealthy}")

    out = {
        "calibration_seeds": [seeds[0], seeds[-1] + 1],
        "profiles": list(profiles),
        "fp_budget": FP_BUDGET,
        "error_rate_threshold": 0.05,
        "degraded_entry": cfg,
        "unhealthy": unhealthy,
        "calibration_mean_f1": round(mean_f1, 4),
        "calibration_pooled_clean_fp": round(float(pooled_fp), 4),
        "note": "Thresholds for engines/trend_rule. Calibrated on seeds disjoint "
                "from the benchmark eval seeds (1..8) and the trend_forest fit "
                "seeds (700..839). Primary binary metrics depend only on the "
                "degraded-entry gates + recovery_slope; unhealthy gates set the "
                "tiering/severity only.",
    }
    out_path = _REPO / "tools" / "anomaly-training" / "trend_rule_calibration.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"[calibrate_trend] wrote -> {out_path}")


if __name__ == "__main__":
    main()
