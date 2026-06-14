# `trend_rule` anomaly engine

Interpretable, **stateful** trend-aware detector. The classical-mode counterpart
to `trend_forest`, and the engine that closes the gradual-degradation gap every
stateless engine (`threshold`, `isolation_forest`, `zscore`) misses.

## Why

The four point features the run loop emits per window (MAX / AVG / STDDEV
latency + error_rate) carry no history, so a backend whose latency drifts slowly
upward looks identical — window by window — to one that is steadily slow: the
within-window *shape* is unchanged, only the level rises relative to the
backend's own normal. With no memory of that normal, gradual degradation scores
~0 recall. This engine adds the missing axis via
[`features/trend.py`](../../features/trend.py).

## How

Per backend, per cycle, three channels (worst wins); each has a degraded and an
unhealthy gate:

- **error** — `error_rate > error_rate_threshold` → unhealthy (no history
  needed; catches error bursts).
- **spike** — window MAX / mean jumps far above the backend's own baseline
  (`max_dev` / `mean_dev`) → trips on the first window.
- **drift** — one-sided CUSUM of the standardised mean deviation (`cusum_pos`)
  → accumulates a slow ramp until it trips, well before any single window looks
  abnormal.

A **recovery suppressor** holds back latency alarms while the trend is steeply
falling (`slope ≤ -recovery_slope`): a backend that is visibly getting better is
recovering, not degrading. This is what clears the post-anomaly window-straddle
tail quickly instead of paging on it.

State is one `TrendExtractor` (keyed by `backend_id`); `reset()` drops it. During
warmup (insufficient history) only the error channel is live, so a cold start
cannot raise a latency alert.

## Thresholds / calibration

Defaults are the output of
[`tools/anomaly-training/calibrate_trend.py`](../../../../tools/anomaly-training/calibrate_trend.py)
(calibration seeds `300..331`, disjoint from the benchmark eval seeds `1..8` and
the `trend_forest` fit seeds), recorded in `trend_rule_calibration.json`. The
primary `status != healthy` boundary depends only on the degraded-entry gates +
`recovery_slope`; the unhealthy gates set tiering/severity only.

## Selecting it

```python
from engine_base import select_engine
engine = select_engine("trend_rule", error_rate_threshold=0.05, min_sample_count=10)
```

Recommended `flip_confirmation_cycles = 2` (the gate sweet spot — see the
benchmark REPORT). Continuous severity for PR-AUC is read via
`engine.last_anomaly_value()` straight after `score()`.

## Results (8-seed benchmark, raw)

| profile | F1 | recall | FP |
|---|---|---|---|
| latency-spike | 0.959 | 0.963 | 0.013 |
| error-burst | 0.892 | 0.928 | 0.047 |
| gradual-degradation | **0.845** | **0.791** | 0.025 |
| partial-failure (held-out) | 0.921 | 1.000 | 0.052 |
| clean-control | — | — | 0.000 |
| flappy-clean (noisy) | — | — | 0.034 |

See `experiments/anomaly-detection-bench/REPORT.md` for the full analysis.
