# `features/` — shared feature extraction

Feature transforms shared by anomaly engines. Kept separate from the engines so
both the rule-based (`trend_rule`) and trained (`trend_forest`) detectors derive
identical signals from the same code.

## `trend.py` — per-backend temporal features

Adds the *history* axis the four point features
(`runloop.build_features_from_rows`: window MAX / AVG / STDDEV latency +
error_rate) lack. Stateless point features can't tell a slow drift from a steady
slow backend (the within-window shape is identical); these can, by measuring
deviation from the backend's **own** established baseline.

`TrendExtractor` holds one `BackendTrendState` per `backend_id` and, per cycle,
returns `TrendFeatures`:

| feature | meaning |
|---|---|
| `mean_dev` | rel. deviation of window mean from a slow, contamination-guarded EWMA baseline |
| `max_dev` | same for the window MAX |
| `cusum_pos` | one-sided CUSUM of the standardised mean deviation (gradual-drift detector) |
| `slope` | OLS trend of recent means (fraction of baseline / step) |
| `max_ratio`, `std_ratio` | within-window shape |

Key properties:

- **Contamination-guarded baseline** — damped/frozen under deviation so it does
  not chase a ramp (a plain EWMA would erase the very signal it measures).
- **CUSUM reset-on-return-to-control** — hard-drains once back at baseline, so
  recovery is fast (no long false-positive tail) without harming accumulation
  during real drift.
- **Warmup** — trend signals are suppressed until `warmup_steps` windows are
  seen, so a cold start never manufactures an alert.
- **`reset()` / `reset_backend()`** — drop state for an independent stream (a
  fresh trace, or a backend coming online).

Constants `POINT_FEATURE_ORDER`, `TREND_FEATURE_ORDER`, `ENRICHED_FEATURE_ORDER`
define the 10-D enriched vector (4 point + 6 trend) the `trend_forest` model is
trained on — keep stable or retrain.

Dependency-light (numpy only) and deterministic. Tests: `test_trend.py`.
