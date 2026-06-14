# anomaly-detection-bench

Detection-quality benchmark for the SmartLoad anomaly engines. Unlike
`anomaly-engine-bench` (which only measures engine-vs-engine *agreement* on a
static feature grid), this harness drives every contender over labeled
synthetic feature streams and scores real detection quality: Precision,
Recall, F1, false-positive rate, detection latency, recovery latency, PR-AUC,
and a 3-tier confusion matrix.

## Contenders

All four are scored on **identical** feature streams:

| Contender | What it is |
|---|---|
| `threshold` | `services/anomaly-detector/engines/threshold/engine.py` — latency-ratio + error-rate rule |
| `isolation_forest_shipped` | the shipped `models/isolation_forest.pkl` — included to demonstrate the degenerate-band defect |
| `isolation_forest_retrained` | the quantile-calibrated `models/isolation_forest_retrained.pkl` (see `tools/anomaly-training/retrain_calibrated.py`) |
| `zscore` | a 3-sigma latency z-score baseline (`zscore_engine.py`, this experiment) |

## Generator

`generators.py` produces deterministic, ground-truth-labeled feature traces
that mirror `runloop.build_features_from_rows` semantics (window MAX / AVG /
STDDEV / error AVG / COUNT). Four profiles:

- `latency-spike` — window MAX jumps to 1.5–5× baseline with spike-proportional variance.
- `error-burst` — `error_rate` steps 0 → {0.08, 0.15, 0.30} during the injection.
- `gradual-degradation` — latency drifts linearly upward over 60–120 s.
- `clean-control` — pure healthy traffic; measures specificity / false-positive rate.

Sample counts per window stay well above the engines' `min_sample_count` gate.

## Gate variants

Every contender is run twice: raw verdict, and wrapped by
`runloop.apply_stability_gate` at `flip_confirmation_cycles ∈ {1, 2, 3}`. The
low-sample hold is capped at `LOW_SAMPLE_HOLD_CAP_CYCLES` so recovery latency
stays bounded (noted in the SUMMARY).

## Run it

```bash
/tmp/sk132env/Scripts/python.exe experiments/anomaly-detection-bench/run.py
python experiments/anomaly-detection-bench/run.py --seeds 8 --tag myrun
```

No docker stack needed — the engines import directly and load the real `.pkl`
artifacts. Uses scikit-learn 1.3.2 / numpy 1.26.4 (the container pin) so the
shipped and retrained artifacts load warning-free.

## Output

`results/<tag>/`:

| File | Contents |
|---|---|
| `grid.csv` | one row per (engine × profile × gate-variant × seed): precision, recall, F1, FP-rate, detect/recover latency, TP/FP/FN/TN |
| `SUMMARY.md` | thesis-ready tables — ALL roll-up, per (profile × gate-variant) blocks, PR-AUC, 3-tier confusion matrices, and the gate-off-vs-gate-on latency/FP-rate headline |
| `meta.json` | sklearn/numpy/pandas versions, seeds, generator params, the retrained model's thresholds + contamination, and the calibration-vs-evaluation seed split |
