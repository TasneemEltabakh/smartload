# arima engine

ARIMA or Prophet forecaster trained on preprocessed Google Borg traces. Replaces the moving-average baseline when `FORECAST_ENGINE=arima` and a model file is present.

## Status

Scaffolded only. Implementation lands when:
- the model is trained (issue #102)
- preprocessed Borg dataset exists (R1.2)

## Planned files

- `engine.py` — `ArimaEngine(ForecastEngine)` that loads the model and falls back to moving-average if missing
- `test_engine.py` — fixture-based tests against held-out traces
