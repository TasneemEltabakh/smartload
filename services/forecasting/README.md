# forecasting

Produces short-horizon RPS forecasts for the autoscaler. Publishes `ForecastResult` to `smartload.forecast`.

## Role
- Polls TimescaleDB every `POLL_INTERVAL_SECONDS` for recent request-rate history
- Runs the configured engine (`harmonic_residual` is the promoted default; `moving_average` is the artifact-free safety fallback)
- Publishes `ForecastResult` to `smartload.forecast`

## Engines
Pluggable — one folder per engine. See `engines/`.
- `harmonic_residual/` — **default.** Robust dynamic-harmonic-regression forecaster with an AR(1) residual correction and split-conformal confidence bands. Beats naive / arima / moving_average on MAPE+sMAPE on every load shape with calibrated bands, and is the only engine that converts into a downstream autoscaler SLA win. Pure NumPy, deterministic, no model artifact. See `experiments/forecasting-engine-bench/REPORT.md`.
- `moving_average/` — artifact-free baseline; rolling mean over last hour. Stays as the never-fails fallback the run loop reverts to if a requested engine cannot construct.
- `arima/` — trained ARIMA(3,0,1) artifact (issue #102). Selectable, but below the < 20% MAPE SLO and trend-blind (`d=0`); superseded as the default by `harmonic_residual`.

Selection: `FORECAST_ENGINE` env var.

## Redis channels
- Subscribes: `smartload.policy`
- Publishes: `smartload.forecast`

## Env vars
- `TIMESCALEDB_URL`, `REDIS_URL`
- `FORECAST_RUNLOOP_ENABLED` (default `true` since v1.0.7g; was `false` before) — set to `false` to revert to the Phase-0 stub (no engine, `/health` only). See SOT §8.6 + issue #138.
- `FORECAST_ENGINE` (default `harmonic_residual`) — `harmonic_residual` | `moving_average` | `arima`. If the requested engine fails to load (e.g. missing `.pkl`), the service falls back to `moving_average` and reports `engine_ready=false` on `/health`.
- `POLL_INTERVAL_SECONDS` (default 60)
- `FORECAST_WINDOW_MINUTES` (default 60) — DB lookback window passed to `FORECAST_QUERY`.

### Scaler-facing look-ahead

`FORECAST_QUERY` buckets the request-rate series at **1 minute**, and a single `forecast()` call is a single-bucket (1-step) projection. For the autoscaler the operationally useful signal is the forecast for the warm-up lead window *ahead*, not one bucket. These knobs run the engine in that look-ahead mode. Their defaults reproduce the single-step accuracy behaviour exactly, so an unset deployment is unchanged.

- `FORECAST_LEAD_STEPS` (default `1`) — look-ahead in `FORECAST_QUERY` buckets (1 min each). `1` → single-step `forecast()`. `>1` → `forecast_ahead(steps=N)` on engines that support it (`harmonic_residual`); engines without `forecast_ahead` (`moving_average`, `arima`) keep their single-step forecast regardless, so flipping this on never crashes the loop or forces a fallback. Deployed value = forecast horizon ÷ bucket cadence (e.g. a 5-minute horizon at 1-min buckets → `5`). The published/persisted `horizon_minutes` is relabelled to the true lead (`FORECAST_LEAD_STEPS` × 1 min) when this path is active.
- `FORECAST_FIT_WINDOW` (default unset → engine's own default) — trailing samples the `harmonic_residual` engine fits per call. The scaler-facing recommendation is a short, **local** window (e.g. `120`); leave unset for the accuracy-optimal long window. Ignored by engines that don't declare it.
- `FORECAST_ROBUST_MODE` (default `symmetric`) — `symmetric` (accuracy-optimal; downweights spikes in both directions) or `downward` (asymmetric, scaler-facing: gives upward spikes full weight so the forecast rises under a flash crowd, trading point accuracy for SLA). Ignored by engines that don't declare it.

The accuracy-optimal default (long window, symmetric, single-step) serves the forecasting service's own accuracy SLOs; the scaler-facing mode (`FORECAST_LEAD_STEPS>1`, `FORECAST_FIT_WINDOW=120`, `FORECAST_ROBUST_MODE=downward`) is the autoscaler's forward signal — both from one engine via configuration. See `experiments/forecasting-engine-bench/REPORT.md` §6–§7.

## /health response

When the run loop is enabled, `/health` adds three engine fields:

```json
{
  "status": "ok",
  "redis": true,
  "timescaledb": true,
  "engine_type": "harmonic_residual",
  "engine_requested": "harmonic_residual",
  "engine_ready": true,
  "last_inference_age_seconds": 41.2
}
```

`engine_ready=false` with `engine_type != engine_requested` means the requested engine couldn't load and the service is running the `moving_average` baseline fallback. Returns 200 unless Redis or TimescaleDB is unreachable (then 503).

## Status

- Phase 0 stub: `/health` only
- Phase 1 run loop (this folder): wired behind `FORECAST_RUNLOOP_ENABLED` (**on by default** since v1.0.7g). `harmonic_residual` is the promoted default engine; `moving_average` is the artifact-free fallback; `arima` remains selectable.

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
