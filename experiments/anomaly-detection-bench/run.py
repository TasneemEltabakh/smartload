"""
experiments/anomaly-detection-bench/run.py
───────────────────────────────────────────
Anomaly-DETECTION benchmark (distinct from anomaly-engine-bench, which only
measures engine-vs-engine agreement on a static grid).

This harness drives six contenders over identical synthetic feature streams
that carry ground-truth labels, and scores real detection quality: Precision,
Recall, F1, false-positive rate, detection latency and recovery latency, plus
PR-AUC for the score-producing engines and a 3-tier confusion matrix per
engine. Every contender is also run through runloop.apply_stability_gate at
flip_confirmation_cycles in {1, 2, 3} so the cost of hysteresis (added
detection latency) and its benefit (suppressed false positives) are measured
directly.

Contenders (all scored on the SAME feature streams):
  threshold                  services/.../engines/threshold/engine.py
  isolation_forest_shipped   the shipped (degenerate-band) artifact — included
                             to demonstrate the defect
  isolation_forest_retrained the quantile-calibrated artifact from
                             tools/anomaly-training/retrain_calibrated.py
  zscore                     a 3-sigma latency-z-score baseline (this experiment)
  trend_rule                 stateful trend-aware rule engine (CUSUM + baseline
                             deviation) — closes the gradual-degradation gap
  trend_forest               IsolationForest on the enriched temporal features
                             (tools/anomaly-training/train_trend.py)

The trend_* engines are STATEFUL (per-backend memory across cycles): they are
reset per trace and scored sequentially, exposing last_anomaly_value() for
PR-AUC. Profiles include two HELD-OUT controls no engine trains/calibrates on:
`partial-failure` (generalization) and `flappy-clean` (gate behaviour on noisy
telemetry). See generators.TRAIN_PROFILES vs HELDOUT_PROFILES.

Primary metrics binarise the 3-tier status as (status != "healthy"), matching
evaluate_live.py. PR-AUC is reported for the score-producing engines.

Usage (scikit-learn 1.3.2 / numpy<2 interpreter — loads the shipped .pkl artifacts):
    .venv/bin/python experiments/anomaly-detection-bench/run.py
    python experiments/anomaly-detection-bench/run.py --seeds 8 --tag myrun
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
_EXPERIMENTS = _HERE.parent
_REPO = _EXPERIMENTS.parent
_SVC = _REPO / "services" / "anomaly-detector"

for _p in (
    str(_EXPERIMENTS),                       # _bench_common
    str(_HERE),                              # generators, zscore_engine
    str(_SVC),                               # engine_base, runloop
    str(_SVC / "engines" / "threshold"),
    str(_SVC / "engines" / "isolation_forest"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sklearn  # noqa: E402

from _bench_common import bench_stats  # noqa: E402
from engine_base import AnomalyScore, BackendFeatures  # noqa: E402
from runloop import BackendState, apply_stability_gate  # noqa: E402

import generators  # noqa: E402
from generators import GenParams, injection_window  # noqa: E402
from zscore_engine import ZScoreEngine  # noqa: E402

SHIPPED_MODEL = _SVC / "models" / "isolation_forest.pkl"
RETRAINED_MODEL = _SVC / "models" / "isolation_forest_retrained.pkl"

# Cap on how long the stability gate's low-sample hold may freeze a backend's
# last non-healthy status. Without a cap, a backend that goes quiet during
# recovery could hold "unhealthy" until run end, making recovery-latency
# unbounded. Capping the hold at a fixed number of cycles bounds it.
LOW_SAMPLE_HOLD_CAP_CYCLES = 5

TREND_FOREST_MODEL = _SVC / "models" / "trend_forest.pkl"

GATE_VARIANTS = ("raw", "gate-1", "gate-2", "gate-3")
SCORE_ENGINES = ("isolation_forest_shipped", "isolation_forest_retrained", "zscore",
                 "trend_rule", "trend_forest")
# Stateful engines carry per-backend memory across cycles and must be reset
# between independent traces; they are scored sequentially (not batched) and
# expose last_anomaly_value() for PR-AUC.
STATEFUL_ENGINES = ("trend_rule", "trend_forest")
# Display / iteration order for the per-engine tables.
ENGINE_ORDER = ("threshold", "isolation_forest_shipped", "isolation_forest_retrained",
                "zscore", "trend_rule", "trend_forest")
TIER_ORDER = ("healthy", "degraded", "unhealthy")


# ── contenders ────────────────────────────────────────────────────────────

def _load_contenders():
    """Instantiate the four contenders. Each exposes .score(features); the
    score-producing ones also expose a continuous anomaly value for PR-AUC."""
    from engines.threshold.engine import ThresholdEngine
    from engines.isolation_forest.engine import IsolationForestEngine
    from engines.trend_rule.engine import TrendRuleEngine
    from engines.trend_forest.engine import TrendForestEngine

    threshold = ThresholdEngine(latency_multiplier=3.0, error_rate_threshold=0.05, min_sample_count=10)
    iso_shipped = IsolationForestEngine(model_path=SHIPPED_MODEL, min_sample_count=10)
    iso_retrained = IsolationForestEngine(model_path=RETRAINED_MODEL, min_sample_count=10)
    zscore = ZScoreEngine(error_rate_threshold=0.05, min_sample_count=10)
    trend_rule = TrendRuleEngine(error_rate_threshold=0.05, min_sample_count=10)
    trend_forest = TrendForestEngine(model_path=TREND_FOREST_MODEL, min_sample_count=10)

    return {
        "threshold": threshold,
        "isolation_forest_shipped": iso_shipped,
        "isolation_forest_retrained": iso_retrained,
        "zscore": zscore,
        "trend_rule": trend_rule,
        "trend_forest": trend_forest,
    }


# ── per-trace scoring ──────────────────────────────────────────────────────

def _to_features(step) -> BackendFeatures:
    return BackendFeatures(
        backend_id="bench",
        latency_ms=step.latency_ms,
        latency_rolling_mean_ms=step.latency_rolling_mean_ms,
        error_rate=step.error_rate,
        sample_count=step.sample_count,
        latency_rolling_std_ms=step.latency_rolling_std_ms,
    )


def _iso_raw_batch(engine, steps) -> tuple[list, list[float | None]]:
    """Vectorised IsolationForest scoring for a whole trace in ONE
    decision_function call, reproducing engine.score()'s decision rule and
    sample-count / non-finite gates exactly.

    Returns (raw AnomalyScores, anomaly values). The anomaly value is the
    negated decision_function (higher = more anomalous) for PR-AUC, or None for
    a gated-out step. This is the hot path — a per-step decision_function call
    on a 200-tree forest is ~100x slower than one batched call."""
    cols = ("latency_ms", "latency_rolling_mean_ms", "error_rate", "latency_rolling_std_ms")
    X = np.array([[getattr(s, c) for c in cols] for s in steps], dtype="float64")
    finite = np.isfinite(X).all(axis=1)
    enough = np.array([s.sample_count >= engine.min_sample_count for s in steps])
    usable = finite & enough

    raw_decision = np.full(len(steps), np.nan)
    if usable.any():
        raw_decision[usable] = engine.model.decision_function(
            engine.production_scaler.transform(X[usable]))

    ha, ub, scale = engine.healthy_above, engine.unhealthy_below, engine.unhealthy_score_scale
    raws: list = []
    values: list[float | None] = []
    for i, step in enumerate(steps):
        if not usable[i]:
            # Mirrors engine.score(): low-sample / non-finite -> healthy/0.0.
            raws.append(AnomalyScore("bench", "healthy", 0.0))
            values.append(None)
            continue
        d = float(raw_decision[i])
        values.append(-d)
        if d > ha:
            raws.append(AnomalyScore("bench", "healthy", 0.0))
        elif d >= ub:
            raws.append(AnomalyScore("bench", "degraded", 0.5,
                                     metric="anomaly_score", observed_value=d, threshold=ha))
        else:
            raws.append(AnomalyScore("bench", "unhealthy", min(1.0, abs(d - ub) / scale),
                                     metric="anomaly_score", observed_value=d, threshold=ub))
    return raws, values


def _raw_scores(engine, name: str, steps) -> tuple[list, list[float | None]]:
    """Compute the engine's raw AnomalyScore per step ONCE, plus the continuous
    anomaly value per step (None for the threshold engine). The raw scores are
    reused across every gate variant, so the model is evaluated once per
    (engine, trace) rather than once per (engine, trace, gate-variant)."""
    if name in ("isolation_forest_shipped", "isolation_forest_retrained"):
        return _iso_raw_batch(engine, steps)
    if name in STATEFUL_ENGINES:
        # Stateful engines carry per-backend memory across cycles, so they must
        # be reset before each independent trace and scored strictly in time
        # order (no batching). score() is the single state-advancing call per
        # cycle; the continuous PR-AUC value is read straight after it via
        # last_anomaly_value() rather than recomputed (which would double-advance
        # the state). reset() mirrors a backend coming online fresh.
        engine.reset()
        raws: list = []
        values: list[float | None] = []
        for s in steps:
            raws.append(engine.score(_to_features(s)))
            values.append(engine.last_anomaly_value())
        return raws, values
    raws = [engine.score(_to_features(s)) for s in steps]
    if name == "zscore":
        values = [engine.anomaly_value(_to_features(s)) for s in steps]
    else:
        values = [None] * len(steps)
    return raws, values


def _apply_gate(raws, min_sample_count: int, steps, gate_variant: str) -> list[str]:
    """Apply one gate variant to a cached list of raw AnomalyScores and return
    the per-step status strings.

    The low-sample hold of apply_stability_gate is capped at
    LOW_SAMPLE_HOLD_CAP_CYCLES consecutive cycles: without a cap a backend that
    goes quiet during recovery could hold 'unhealthy' to run end, making
    recovery-latency unbounded."""
    if gate_variant == "raw":
        return [r.status for r in raws]
    cycles = {"gate-1": 1, "gate-2": 2, "gate-3": 3}[gate_variant]
    state = BackendState()
    verdicts: list[str] = []
    for raw, step in zip(raws, steps):
        low_sample = step.sample_count < min_sample_count
        verdicts.append(apply_stability_gate(
            raw, low_sample, state, cycles,
            max_hold_cycles=LOW_SAMPLE_HOLD_CAP_CYCLES).status)
    return verdicts


# ── metrics ────────────────────────────────────────────────────────────────

def _detection_metrics(verdicts, labels, steps, params: GenParams):
    """Precision / recall / F1 / FP-rate plus detection- and recovery-latency
    (in seconds) for one scored trace.

    Binarisation: pred = (status != 'healthy'); truth = label.
    Detection latency: seconds from injection start to the first non-healthy
      emission whose window has entered the injection (NaN if never tripped or
      no injection).
    Recovery latency: seconds from injection end to the first healthy emission
      after the trace's last anomalous label (NaN if it never clears — i.e.
      censored at run end — or no injection)."""
    pred = [v != "healthy" for v in verdicts]
    truth = [bool(l) for l in labels]

    tp = sum(p and t for p, t in zip(pred, truth))
    fp = sum(p and not t for p, t in zip(pred, truth))
    fn = sum((not p) and t for p, t in zip(pred, truth))
    tn = sum((not p) and (not t) for p, t in zip(pred, truth))

    precision = tp / (tp + fp) if (tp + fp) else math.nan
    recall = tp / (tp + fn) if (tp + fn) else math.nan
    f1 = (2 * precision * recall / (precision + recall)
          if precision and recall and not math.isnan(precision) and not math.isnan(recall) else
          (0.0 if (tp + fp + fn) else math.nan))
    fp_rate = fp / (fp + tn) if (fp + tn) else math.nan

    has_injection = any(truth)
    inj_start, inj_end = injection_window(params)

    detect_latency = math.nan
    recover_latency = math.nan
    if has_injection:
        # detection: first non-healthy emission at or after injection start.
        for v, s in zip(verdicts, steps):
            if s.t_s - 1 >= inj_start and v != "healthy":
                detect_latency = float(max(0, (s.t_s - 1) - inj_start))
                break
        # recovery: first healthy emission strictly after the last labelled-
        # anomalous emission. Censored (NaN) if it never clears by run end.
        last_anom_t = max((s.t_s for s, l in zip(steps, labels) if l), default=None)
        if last_anom_t is not None:
            for v, s in zip(verdicts, steps):
                if s.t_s > last_anom_t and v == "healthy":
                    recover_latency = float(max(0, s.t_s - inj_end))
                    break

    confusion = {t: {"healthy": 0, "degraded": 0, "unhealthy": 0} for t in ("clean", "anom")}
    for v, t in zip(verdicts, truth):
        confusion["anom" if t else "clean"][v] += 1

    return {
        "precision": precision, "recall": recall, "f1": f1, "fp_rate": fp_rate,
        "detect_latency_s": detect_latency, "recover_latency_s": recover_latency,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn, "confusion": confusion,
    }


def _pr_auc(values, labels) -> float:
    """Average-precision-style PR-AUC from continuous anomaly values.

    Steps with value None (gated-out / low-sample) are dropped. Returns NaN if
    there is no positive or the values are all None. Implemented as the area
    under the precision-recall curve via the trapezoidal rule over sorted
    thresholds, equivalent to sklearn.metrics.auc(recall, precision)."""
    pairs = [(v, l) for v, l in zip(values, labels) if v is not None]
    if not pairs:
        return math.nan
    vals = np.array([p[0] for p in pairs], dtype="float64")
    labs = np.array([p[1] for p in pairs], dtype="int64")
    n_pos = int(labs.sum())
    if n_pos == 0:
        return math.nan

    order = np.argsort(-vals)  # descending score
    labs = labs[order]
    tp = np.cumsum(labs)
    fp = np.cumsum(1 - labs)
    precision = tp / np.maximum(tp + fp, 1)
    recall = tp / n_pos
    # Prepend the (recall=0, precision=1) origin so the curve integrates from 0.
    recall = np.concatenate([[0.0], recall])
    precision = np.concatenate([[1.0], precision])
    return float(np.trapz(precision, recall))


# ── driver ──────────────────────────────────────────────────────────────────

def main(seeds: int, tag: str | None) -> None:
    params = GenParams()
    contenders = _load_contenders()
    seed_list = list(range(1, seeds + 1))

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = tag or ts
    out_dir = _HERE / "results" / tag
    out_dir.mkdir(parents=True, exist_ok=True)

    # Long/tidy per-run rows for grid.csv + CI aggregation.
    grid_rows: list[dict] = []
    # PR-AUC per (engine, profile) per seed (ungated, score engines only).
    prauc_rows: list[dict] = []
    # Confusion accumulation per (engine, gate_variant): summed over all
    # profiles+seeds, for the 3-tier confusion matrix tables.
    confusion_acc: dict[tuple[str, str], dict] = {}

    for profile in generators.PROFILES:
        for seed in seed_list:
            steps = generators.generate(profile, seed, params)
            labels = [s.label for s in steps]
            for name, engine in contenders.items():
                # Raw scores + continuous values computed ONCE per (engine,
                # trace); every gate variant reuses them.
                raws, values = _raw_scores(engine, name, steps)
                for gate_variant in GATE_VARIANTS:
                    verdicts = _apply_gate(raws, engine.min_sample_count, steps, gate_variant)
                    m = _detection_metrics(verdicts, labels, steps, params)
                    grid_rows.append({
                        "engine": name, "profile": profile, "gate_variant": gate_variant,
                        "seed": seed,
                        "precision": m["precision"], "recall": m["recall"], "f1": m["f1"],
                        "fp_rate": m["fp_rate"],
                        "detect_latency_s": m["detect_latency_s"],
                        "recover_latency_s": m["recover_latency_s"],
                        "tp": m["tp"], "fp": m["fp"], "fn": m["fn"], "tn": m["tn"],
                    })
                    key = (name, gate_variant)
                    acc = confusion_acc.setdefault(
                        key, {t: {"healthy": 0, "degraded": 0, "unhealthy": 0} for t in ("clean", "anom")})
                    for cls in ("clean", "anom"):
                        for tier in TIER_ORDER:
                            acc[cls][tier] += m["confusion"][cls][tier]

                # PR-AUC: ungated continuous values, score engines only.
                if name in SCORE_ENGINES:
                    auc = _pr_auc(values, labels)
                    prauc_rows.append({
                        "engine": name, "profile": profile, "seed": seed, "pr_auc": auc,
                    })

    _write_grid_csv(out_dir, grid_rows)
    summary = _build_summary(grid_rows, prauc_rows, confusion_acc, params, seed_list)
    (out_dir / "SUMMARY.md").write_text(summary, encoding="utf-8")
    _write_meta(out_dir, params, seed_list)

    print(f"[anomaly-detection-bench] {len(grid_rows)} run-rows over "
          f"{len(generators.PROFILES)} profiles x {len(seed_list)} seeds x "
          f"{len(contenders)} engines x {len(GATE_VARIANTS)} gate-variants")
    print(f"[anomaly-detection-bench] wrote -> {out_dir}")


def _write_grid_csv(out_dir: Path, rows: list[dict]) -> None:
    import csv
    fields = ["engine", "profile", "gate_variant", "seed", "precision", "recall",
              "f1", "fp_rate", "detect_latency_s", "recover_latency_s",
              "tp", "fp", "fn", "tn"]
    with (out_dir / "grid.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if (isinstance(r[k], float) and math.isnan(r[k])) else r[k]) for k in fields})


# ── summary rendering ────────────────────────────────────────────────────────

import pandas as pd  # noqa: E402


def _ci_cell(df, engine, metric, decimals=3, unit="") -> str:
    sub = df[df["engine"] == engine][metric].tolist()
    stat = bench_stats.mean_ci(sub)
    return bench_stats.format_mean_ci(stat["mean"], stat["half_width"], stat["n"],
                                      decimals=decimals, unit=unit)


def _engine_table(df) -> list[str]:
    """One Engine | Precision | Recall | F1 | FP-rate | Detect | Recover block
    for a given (already-filtered) sub-frame, mean ± CI over seeds."""
    lines = [
        "| Engine | Precision | Recall | F1 | FP-rate | Detect-latency_s | Recover-latency_s |",
        "|---|---|---|---|---|---|---|",
    ]
    for engine in ENGINE_ORDER:
        if engine not in set(df["engine"]):
            continue
        lines.append(
            f"| `{engine}` "
            f"| {_ci_cell(df, engine, 'precision')} "
            f"| {_ci_cell(df, engine, 'recall')} "
            f"| {_ci_cell(df, engine, 'f1')} "
            f"| {_ci_cell(df, engine, 'fp_rate')} "
            f"| {_ci_cell(df, engine, 'detect_latency_s', decimals=1)} "
            f"| {_ci_cell(df, engine, 'recover_latency_s', decimals=1)} |"
        )
    return lines


def _build_summary(grid_rows, prauc_rows, confusion_acc, params: GenParams, seed_list) -> str:
    df = pd.DataFrame(grid_rows)
    inj_start, inj_end = injection_window(params)

    lines: list[str] = [
        "# Anomaly-Detection Benchmark — Results",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat()}.",
        "",
        f"- Profiles: `{', '.join(generators.PROFILES)}`",
        f"- Seeds per profile: {len(seed_list)} (`{seed_list[0]}..{seed_list[-1]}`)",
        f"- Trace: {params.total_seconds}s, {params.window_s}s window emitted every "
        f"{params.step_s}s, ~{params.requests_per_second} req/s",
        f"- Injection: t={inj_start}..{inj_end}s ({params.duration_s}s active)",
        f"- Primary binarisation: `status != \"healthy\"` (matches evaluate_live.py)",
        f"- Stability gate: `runloop.apply_stability_gate` at "
        f"flip_confirmation_cycles in {{1,2,3}}; low-sample hold capped at "
        f"{LOW_SAMPLE_HOLD_CAP_CYCLES} cycles so recovery-latency stays bounded.",
        f"- sklearn {sklearn.__version__}, numpy {np.__version__}",
        "",
        "Cells are mean ± 95% t-CI over seeds. Lead metrics are **F1** and "
        "**FP-rate**. Detect-/recover-latency are seconds; recover-latency is "
        "NaN-censored when a backend never clears by run end (shown blank).",
        "",
        "---",
        "",
        "## ALL roll-up (every profile x gate-variant pooled)",
        "",
    ]
    lines += _engine_table(df)
    lines.append("")

    # PR-AUC (score engines, ungated).
    if prauc_rows:
        pdf = pd.DataFrame(prauc_rows)
        lines += ["", "## PR-AUC (score-producing engines, ungated, mean ± CI over seeds)", ""]
        lines += ["| Engine | " + " | ".join(generators.PROFILES) + " | ALL |",
                  "|---|" + "---|" * (len(generators.PROFILES) + 1)]
        for engine in SCORE_ENGINES:
            cells = []
            for profile in generators.PROFILES:
                sub = pdf[(pdf["engine"] == engine) & (pdf["profile"] == profile)]["pr_auc"].tolist()
                st = bench_stats.mean_ci(sub)
                cells.append(bench_stats.format_mean_ci(st["mean"], st["half_width"], st["n"], decimals=3))
            allsub = pdf[pdf["engine"] == engine]["pr_auc"].tolist()
            sta = bench_stats.mean_ci(allsub)
            cells.append(bench_stats.format_mean_ci(sta["mean"], sta["half_width"], sta["n"], decimals=3))
            lines.append(f"| `{engine}` | " + " | ".join(cells) + " |")
        lines.append("")
        lines.append("_(PR-AUC is NaN for `clean-control` — no positives — and shown as `—`.)_")

    # Per (profile x gate-variant) blocks.
    lines += ["", "---", "", "## Per profile x gate-variant"]
    for profile in generators.PROFILES:
        lines += ["", f"### Profile: `{profile}`"]
        for gate_variant in GATE_VARIANTS:
            sub = df[(df["profile"] == profile) & (df["gate_variant"] == gate_variant)]
            if sub.empty:
                continue
            lines += ["", f"**gate: `{gate_variant}`**", ""]
            lines += _engine_table(sub)

    # 3-tier confusion matrices (raw verdicts; clean vs anomalous rows).
    lines += ["", "---", "",
              "## 3-tier confusion matrices (raw, pooled over all profiles+seeds)",
              "",
              "Rows = ground truth (clean / anomalous), columns = emitted tier. "
              "The `degraded` column is the headline: it is **0 for the shipped "
              "Isolation Forest** (its band collapsed, so that tier is "
              "unreachable) and **nonzero for the retrained one**.",
              ""]
    for engine in ENGINE_ORDER:
        acc = confusion_acc.get((engine, "raw"))
        if acc is None:
            continue
        lines += [f"**`{engine}`**", "",
                  "| truth \\ tier | healthy | degraded | unhealthy |",
                  "|---|---:|---:|---:|"]
        for cls, label in (("clean", "clean (truth=healthy)"), ("anom", "anomalous (truth=anomaly)")):
            row = acc[cls]
            lines.append(f"| {label} | {row['healthy']} | {row['degraded']} | {row['unhealthy']} |")
        lines.append("")

    # Headline: gate-off vs gate-on detection-latency + FP-rate delta.
    lines += ["---", "",
              "## Headline: what the stability gate costs and saves", "",
              "Pooled over all profiles+seeds. The gate's design trade is "
              "detection latency (it confirms a flip over N cycles before "
              "publishing it) against false positives (it absorbs single-cycle "
              "flips). On these stable-step traces the latency cost is the "
              "dominant visible effect; the FP saving shows up only where an "
              "engine actually flaps on clean traffic (z-score, below). "
              "`gate-1` equals `raw` (one cycle confirms immediately).", "",
              "| Engine | Gate | F1 | FP-rate | Detect-latency_s | Recover-latency_s |",
              "|---|---|---|---|---|---|"]
    for engine in ENGINE_ORDER:
        for gate_variant in GATE_VARIANTS:
            sub = df[(df["engine"] == engine) & (df["gate_variant"] == gate_variant)]
            if sub.empty:
                continue
            lines.append(
                f"| `{engine}` | {gate_variant} "
                f"| {_ci_cell(sub, engine, 'f1')} "
                f"| {_ci_cell(sub, engine, 'fp_rate')} "
                f"| {_ci_cell(sub, engine, 'detect_latency_s', decimals=1)} "
                f"| {_ci_cell(sub, engine, 'recover_latency_s', decimals=1)} |"
            )

    # Explicit gate-off vs gate-3 deltas for the retrained engine (the figure).
    # Note: confirmation_cycles=1 confirms a flip on its first observation, so
    # gate-1 is identical to raw by construction — the gate only begins to act
    # at >= 2 cycles.
    lines += ["", "### Gate-off vs gate-on delta (isolation_forest_retrained)", "",
              "`gate-1` is identical to `raw`: one confirmation cycle confirms a "
              "flip immediately, so the gate is a no-op until >= 2 cycles.", ""]

    def _mean(frame, col):
        return bench_stats.mean_ci(frame[col].tolist())["mean"]

    def _signed(delta: float) -> str:
        return f"+{delta:.3f}" if delta >= 0 else f"{delta:.3f}"

    raw = df[(df["engine"] == "isolation_forest_retrained") & (df["gate_variant"] == "raw")]
    g3 = df[(df["engine"] == "isolation_forest_retrained") & (df["gate_variant"] == "gate-3")]
    if not raw.empty and not g3.empty:
        d_fp = _mean(g3, "fp_rate") - _mean(raw, "fp_rate")
        d_det = _mean(g3, "detect_latency_s") - _mean(raw, "detect_latency_s")
        lines += [
            f"Pooled over all profiles:",
            f"- FP-rate: {_mean(raw,'fp_rate'):.3f} (raw) -> {_mean(g3,'fp_rate'):.3f} "
            f"(gate-3), delta **{_signed(d_fp)}**.",
            f"- Detect-latency: {_mean(raw,'detect_latency_s'):.1f}s (raw) -> "
            f"{_mean(g3,'detect_latency_s'):.1f}s (gate-3), delta **+{d_det:.1f}s**.",
            "",
        ]

    # The gate's FP-suppression value is clearest on clean-control, where every
    # non-healthy verdict is a false positive and the gate's job is to absorb
    # transient flips. On these stable, low-noise traces there is little
    # flapping to suppress, so the gate mostly adds detection latency without a
    # large FP saving — an honest regime caveat: hysteresis pays off under noisy
    # input, not under clean steps.
    cc_raw = df[(df["gate_variant"] == "raw") & (df["profile"] == "clean-control")]
    cc_g3 = df[(df["gate_variant"] == "gate-3") & (df["profile"] == "clean-control")]
    if not cc_raw.empty and not cc_g3.empty:
        for engine in ("threshold", "isolation_forest_retrained", "zscore"):
            r = cc_raw[cc_raw["engine"] == engine]
            g = cc_g3[cc_g3["engine"] == engine]
            if r.empty or g.empty:
                continue
            d = _mean(g, "fp_rate") - _mean(r, "fp_rate")
            lines.append(
                f"- clean-control FP-rate `{engine}`: {_mean(r,'fp_rate'):.3f} (raw) "
                f"-> {_mean(g,'fp_rate'):.3f} (gate-3), delta **{_signed(d)}**.")
        lines.append("")
        lines.append(
            "Caveat: these generator traces are stable steps, not flappy "
            "telemetry, so the gate's FP-suppression upside is understated here; "
            "its cost (added detection latency) is fully visible. The gate is "
            "still worth running for the noisy-input regime it was built for.")

    lines.append("")
    return "\n".join(lines)


def _write_meta(out_dir: Path, params: GenParams, seed_list) -> None:
    import joblib
    retrained = joblib.load(RETRAINED_MODEL)
    shipped = joblib.load(SHIPPED_MODEL)
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sklearn_version": sklearn.__version__,
        "numpy_version": np.__version__,
        "pandas_version": pd.__version__,
        "seeds": seed_list,
        "profiles": list(generators.PROFILES),
        "gate_variants": list(GATE_VARIANTS),
        "low_sample_hold_cap_cycles": LOW_SAMPLE_HOLD_CAP_CYCLES,
        "generator_params": params.as_dict(),
        "binarisation": "status != 'healthy'",
        "calibration_vs_evaluation_seed_split": (
            "The retrained model fits + calibrates thresholds on a calibration "
            f"draw (calibration_seed={retrained['metadata'].get('calibration_seed')}); "
            f"its held-out test_f1 uses a separate evaluation_seed="
            f"{retrained['metadata'].get('evaluation_seed')}. This benchmark's traces "
            f"use seeds {seed_list[0]}..{seed_list[-1]}, disjoint from both, so the "
            "model never scores data drawn from its own calibration/evaluation seeds."
        ),
        "retrained_model": {
            "path": str(RETRAINED_MODEL),
            "contamination": retrained["metadata"].get("contamination"),
            "thresholds": retrained["thresholds"],
            "decision_band_width": retrained["metadata"].get("decision_band_width"),
            "test_f1": retrained["metadata"].get("test_f1"),
            "ha_quantile": retrained["metadata"].get("ha_quantile"),
            "ub_quantile": retrained["metadata"].get("ub_quantile"),
        },
        "shipped_model": {
            "path": str(SHIPPED_MODEL),
            "contamination": shipped["metadata"].get("contamination"),
            "thresholds": shipped["thresholds"],
            "decision_band_width": round(
                shipped["thresholds"]["healthy_above"] - shipped["thresholds"]["unhealthy_below"], 6),
            "test_f1": shipped["metadata"].get("test_f1", None),
        },
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Anomaly-detection benchmark over labeled synthetic streams")
    ap.add_argument("--seeds", type=int, default=8, help="number of seeds per profile (default: 8)")
    ap.add_argument("--tag", default=None, help="results subdir name (default: UTC timestamp)")
    args = ap.parse_args()
    main(args.seeds, args.tag)
