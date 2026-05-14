# forecasting

Produces short-horizon RPS forecasts for the autoscaler. Publishes `ForecastResult` to `smartload.forecast`.

## Role
- Polls TimescaleDB every `POLL_INTERVAL_SECONDS` for recent request-rate history
- Runs the configured engine (`moving_average` baseline today; `arima` / Prophet swap planned)
- Publishes `ForecastResult` to `smartload.forecast`

## Engines
Pluggable — one folder per engine. See `engines/`.
- `moving_average/` — baseline; rolling mean over last hour
- `arima/` — stub today

Selection: `FORECAST_ENGINE` env var.

## Redis channels
- Publishes: `smartload.forecast`

## Env vars
- `TIMESCALEDB_URL`, `REDIS_URL`
- `FORECAST_ENGINE` (default `moving_average`)
- `POLL_INTERVAL_SECONDS` (default 60)

## Status
Service skeleton shipped (Phase 0). Engine implementations land in `engines/`.

## See also
- Feature manifest: `docs/features/forecast-autoscale.md` (pending)
- Issues: #22 (moving average), #102 (ARIMA / Prophet)
