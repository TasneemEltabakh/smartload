# threshold engine

Rule-based anomaly classification. The baseline engine; never the trained model.

## Rules

- If error rate exceeds `error_rate_threshold` → `unhealthy`
- If current latency exceeds `latency_multiplier × rolling_mean` → `degraded`
- Otherwise → `healthy`

## Why it ships

This is the fallback. If the trained model file is missing or fails to load at startup, the service falls back to this engine and stays useful. It's also the engine that runs during early development before any model is trained.

## Tuning

All thresholds are read from the operating policy (`anomaly_latency_multiplier`, `error_rate_threshold`) and updated live via `smartload.policy`.

## Tests

- `test_engine.py` — covers each branch of the classification rules.
