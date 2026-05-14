# Unit tests

Pure-function tests organised one folder per service. No I/O, no Redis, no TimescaleDB.

## Layout

- `policy-manager/` — validation rules, audit row shaping
- `autoscaler/` — scaling decision logic against synthetic forecasts
- `anomaly-detector/` — run-loop logic with mock engines (engine-specific unit tests live next to each engine, e.g. `services/anomaly-detector/engines/threshold/test_engine.py`)
- `forecasting/` — same shape
- `rl-engine/` — same shape

## Running

```bash
pytest tests/unit/
```

## Why some tests live inside services/

Per-plugin tests (`services/<svc>/engines/<eng>/test_engine.py`) live with their plugin so that "a plugin is missing tests" is visible by browsing the plugin folder. The convention is enforced by `scripts/lint-structure.py`.
