# moving_average engine

Rolling-mean RPS forecaster. The baseline that ships before ARIMA / Prophet.

## Behavior

Averages the last `window_samples` request-rate observations to predict the next `horizon_minutes`. Confidence band is one standard deviation.

## Why it ships

Same reason as threshold: fallback safety. If the trained forecaster file is missing or fails to load, the service falls back here and the autoscaler keeps receiving signal.

## Tests

- `test_engine.py` — predictions track the rolling mean; confidence band widens with variance.
