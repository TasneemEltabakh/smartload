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
- Subscribes: `smartload.policy`
- Publishes: `smartload.forecast`

## Env vars
- `TIMESCALEDB_URL`, `REDIS_URL`
- `FORECAST_RUNLOOP_ENABLED` (default `true` since v1.0.7g; was `false` before) — set to `false` to revert to the Phase-0 stub (no engine, `/health` only). See SOT §8.6 + issue #138.
- `FORECAST_ENGINE` (default `moving_average`) — `moving_average` | `arima`. If the requested engine fails to load (e.g. missing `.pkl`), the service falls back to `moving_average` and reports `engine_ready=false` on `/health`.
- `POLL_INTERVAL_SECONDS` (default 60)
- `FORECAST_WINDOW_MINUTES` (default 60) — DB lookback window passed to `FORECAST_QUERY`.

## /health response

When the run loop is enabled, `/health` adds three engine fields:

```json
{
  "status": "ok",
  "redis": true,
  "timescaledb": true,
  "engine_type": "moving_average",
  "engine_requested": "arima",
  "engine_ready": false,
  "last_inference_age_seconds": 41.2
}
```

`engine_ready=false` with `engine_type != engine_requested` means the requested engine couldn't load and the service is running the baseline fallback. Returns 200 unless Redis or TimescaleDB is unreachable (then 503).

## Status

- Phase 0 stub: `/health` only — **default**
- Phase 1 run loop (this folder): wired behind `FORECAST_RUNLOOP_ENABLED`. Moving-average baseline ships; ARIMA plugin scaffolded, awaits revised model handoff (#102, see PR #144 review).

## Integrating a trained model

To swap in a trained model:
1. Drop the artifact at `services/forecasting/models/<engine>.pkl`.
2. Implement `engines/<engine>/engine.py` exporting `class <Name>Engine(ForecastEngine)` that loads the artifact in `__init__` and implements `forecast(history) -> Forecast`.
3. Register the engine name in `engine_base.select_engine()`.
4. Set `FORECAST_ENGINE=<engine>` in the deployment env.

The run loop, Redis publishing, policy subscription, and fallback-to-baseline are all owned by `app.py` + `runloop.py` — the engine author only writes the inference function.

## See also
- Feature manifest: `docs/features/forecasting.md` (pending — see SOT §25.9 slice catalog)
- Issues: #138 (engine-wrapper foundation), #102 (ARIMA / Prophet model), PR #144 review
