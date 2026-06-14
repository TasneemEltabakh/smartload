# trend_forest — trained temporal anomaly engine

A stateful, trained anomaly engine: a scikit-learn `IsolationForest` scored over
an **enriched** feature vector — the four point features the run loop emits plus
the six backend-relative temporal signals derived by
`services/anomaly-detector/features/trend.py`.

It is the trained counterpart to the interpretable `trend_rule` engine, and the
trained analogue of `isolation_forest`. The extra temporal signals carry the
per-backend history that a point-feature model has no access to — which is what
lets it detect **gradual degradation** (a slow latency ramp), the failure mode
every stateless engine scores at ~0 recall.

## Feature vector (`ENRICHED_FEATURE_ORDER`)

```
latency_ms              window MAX latency        (point)
latency_rolling_mean_ms window AVG latency        (point)
error_rate              window AVG error rate     (point)
latency_rolling_std_ms  window STDDEV latency     (point)
mean_dev                window mean deviation from the backend's own baseline
max_dev                 window MAX deviation from the backend's own baseline
cusum_pos               one-sided CUSUM of standardised mean deviation (drift)
slope                   OLS slope of recent means, fraction-of-baseline per step
max_ratio               MAX / mean — within-window shape
std_ratio               STD / mean — within-window dispersion
```

## Statefulness

The engine owns one `TrendExtractor` (keyed internally by `backend_id`).
`score()` calls `extractor.update(...)` **exactly once per cycle** — the single
state-advancing call — then reads the derived signals. `reset()` drops all
per-backend state for an independent trace or a backend coming online fresh.
During warmup the four history-dependent trend signals are 0.0 (the extractor
suppresses them), so a cold start never manufactures an alert.

## Verdict tiering

Mirrors `isolation_forest`: `decision_function -> raw`.

| condition                              | status    | score                                            |
|----------------------------------------|-----------|--------------------------------------------------|
| `raw > healthy_above`                  | healthy   | 0.0                                              |
| `unhealthy_below <= raw <= healthy_above` | degraded  | 0.5                                           |
| `raw < unhealthy_below`                | unhealthy | `min(1, |raw - unhealthy_below| / score_scale)`  |

A low-sample window (`sample_count < min_sample_count`) or a non-finite feature
vector returns healthy 0.0, exactly like the point-feature engines. The
continuous severity `last_anomaly_value()` (= `-raw`, higher is more anomalous)
is exposed for PR-AUC; it is set to a low constant for suppressed windows.

## Bundle

The `.pkl` is a bundle dict written by `tools/anomaly-training/train_trend.py`:
`{model, smd_scaler, production_scaler, feature_order, thresholds, metadata}`.
`feature_order` must equal `ENRICHED_FEATURE_ORDER` or the engine raises
`ValueError` on load (so bootstrap can fall back to the rule engine).

The two thresholds are placed by **quantiles** of `decision_function` over a
held-out calibration set, then tuned over a small quantile grid to maximise
binary F1 (`status != healthy`) subject to a clean-control false-positive-rate
constraint. Fit / calibration / evaluation seeds are disjoint from the benchmark
evaluation seeds and the rule-engine calibration seeds — no leakage.

## Retraining

```
.venv/bin/python tools/anomaly-training/train_trend.py
```

Writes `services/anomaly-detector/models/trend_forest.pkl`, prints the chosen
thresholds, a per-profile held-out F1 table, and PASS/FAIL on the acceptance
gates (non-degenerate band, reachable degraded tier, gradual-degradation
recall > 0.3, clean-control FP-rate <= 0.06).
