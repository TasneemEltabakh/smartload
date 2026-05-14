# anomaly-detector

Flags backends as healthy / degraded / unhealthy based on latency + error-rate features. Publishes `AnomalyEvent` envelopes to `smartload.anomaly` so the routing plane can exclude degraded backends.

## Role
- Polls TimescaleDB every `POLL_INTERVAL_SECONDS` for per-backend latency + error rate
- Runs the configured engine (`threshold` baseline today; `isolation_forest` swap planned)
- Publishes `AnomalyEvent` to `smartload.anomaly`
- Subscribes to `smartload.policy` to read `anomaly_latency_multiplier` live

## Engines
Pluggable — one folder per engine. See `engines/`.
- `threshold/` — baseline; flags when `current_latency > multiplier × rolling_mean` OR `error_rate > threshold`
- `isolation_forest/` — stub today; trained model drop-in planned

Selection: `ANOMALY_ENGINE` env var.

## Redis channels
- Subscribes: `smartload.policy`
- Publishes: `smartload.anomaly`

## Env vars
- `TIMESCALEDB_URL`, `REDIS_URL`
- `ANOMALY_ENGINE` (default `threshold`)
- `POLL_INTERVAL_SECONDS` (default 5)

## Status
Service skeleton shipped (Phase 0). Engine implementations land in `engines/`.

## See also
- Feature manifest: `docs/features/anomaly-routing.md` (pending)
- Issues: #96 (threshold), #101 (isolation forest)
