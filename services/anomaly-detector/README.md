# anomaly-detector

Flags backends as healthy / degraded / unhealthy based on latency + error-rate features. Publishes `AnomalyEvent` envelopes to `smartload.anomaly` so the routing plane can exclude degraded backends.

## Role
- Polls TimescaleDB every `POLL_INTERVAL_SECONDS` for per-backend latency + error rate
- Runs the configured engine (`trend_rule` default; others selectable)
- Publishes `AnomalyEvent` to `smartload.anomaly`
- Subscribes to `smartload.policy` to read `anomaly_latency_multiplier` live

## Engines
Pluggable — one folder per engine. See `engines/`.
- `trend_rule/` — **default**; interpretable, stateful trend-aware rule engine over per-backend temporal features (`features/trend.py`). Three channels (error / spike / drift) with a CUSUM drift detector and a recovery suppressor. Closes the gradual-degradation gap that the stateless engines miss; needs no model artifact. Run at `flip_confirmation_cycles = 2` (the stability-gate sweet spot).
- `threshold/` — stateless baseline; flags when `current_latency > multiplier × rolling_mean` OR `error_rate > threshold`
- `trend_forest/` — selectable; trained IsolationForest over the enriched temporal vector (`features/trend.py` + the four point features). Validates that the temporal feature engineering carries to a learned model.
- `isolation_forest/` — selectable; trained point-feature model (#101). Falls back to `threshold` if the `.pkl` is missing.

Selection: `ANOMALY_ENGINE` env var.

## Redis channels
- Subscribes: `smartload.policy`
- Publishes: `smartload.anomaly`

## Env vars
- `TIMESCALEDB_URL`, `REDIS_URL`
- `ANOMALY_RUNLOOP_ENABLED` (default `true` since v1.0.7g; was `false` before) — set to `false` to revert to the Phase-0 stub (no engine, `/health` only). See SOT §8.5 + issue #138.
- `ANOMALY_ENGINE` (default `trend_rule`) — `trend_rule` | `threshold` | `trend_forest` | `isolation_forest`. If the requested engine fails to load (e.g. missing `.pkl`), the service falls back to `threshold` and reports `engine_ready=false` on `/health`.
- `ANOMALY_FLIP_CONFIRMATION_CYCLES` (default 2) — cycles a raw status change must persist before the stability gate (`runloop.apply_stability_gate`) confirms it. Seeds the startup policy; a `smartload.policy` publish overrides it live via `anomaly_flip_confirmation_cycles`.
- `POLL_INTERVAL_SECONDS` (default 10)
- `ANOMALY_WINDOW_SECONDS` (default 60) — DB lookback window passed to `ANOMALY_QUERY`.
- `ANOMALY_TELEMETRY_SERVICE` (default `load-balancer`) — the `service` filter applied to the metrics query.

## /health response

When the run loop is enabled, `/health` adds three engine fields:

```json
{
  "status": "ok",
  "redis": true,
  "timescaledb": true,
  "engine_type": "trend_rule",
  "engine_requested": "trend_rule",
  "engine_ready": true,
  "last_inference_age_seconds": 8.4
}
```

`engine_ready=false` with `engine_type != engine_requested` means the requested engine couldn't load and the service is running the baseline (`threshold`) fallback. Returns 200 unless Redis or TimescaleDB is unreachable (then 503).

## Status

- Phase 1 run loop (this folder): wired behind `ANOMALY_RUNLOOP_ENABLED` (default `true`). `trend_rule` is the deployed default engine, run at `flip_confirmation_cycles = 2`; `threshold`, `trend_forest`, and `isolation_forest` are selectable. The run loop publishes to `smartload.anomaly`, subscribes to `smartload.policy`, and falls back to `threshold` if a requested engine can't load.
- Phase 0 stub: `/health` only — set `ANOMALY_RUNLOOP_ENABLED=false` to revert.

## Integrating a trained model

To swap in a trained model:
1. Drop the artifact at `services/anomaly-detector/models/<engine>.pkl`.
2. Implement `engines/<engine>/engine.py` exporting `class <Name>Engine(AnomalyEngine)` that loads the artifact in `__init__` and implements `score(features) -> AnomalyScore`.
3. Register the engine name in `engine_base.select_engine()`.
4. Set `ANOMALY_ENGINE=<engine>` in the deployment env.

The run loop, Redis publishing, policy subscription, and fallback-to-baseline are all owned by `app.py` + `runloop.py` — the engine author only writes the inference function.

## See also
- Feature manifest: `docs/features/anomaly-detection.md` (pending — see SOT §25.9 slice catalog)
- Issues: #138 (engine-wrapper foundation), #101 (Isolation Forest model)
