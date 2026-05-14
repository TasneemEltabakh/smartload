# autoscaler

Adjusts the backend pool size in response to forecast signals and reactive load measurements.

## Role
- Subscribes to `smartload.forecast` for predicted-RPS signals
- Subscribes to `smartload.policy` for live `min_backends` / `max_backends` / `per_instance_capacity_rps` updates
- Scales the `test-backend` Docker pool out / in using the Docker SDK
- Enforces a cooldown between actions
- Writes every action to the `scaling_events` hypertable
- Publishes `ScaleEvent` to `smartload.scale`
- Reactive fallback: if no forecast has been seen for `FORECAST_STALE_SECONDS`, scales based on current load only

## HTTP endpoints
- `GET /health` — uniform health
- `POST /api/v1/scale` (planned) — manual scale override

## Redis channels
- Subscribes: `smartload.forecast`, `smartload.policy`
- Publishes: `smartload.scale`

## Env vars
- `TIMESCALEDB_URL`, `REDIS_URL`
- `MIN_BACKENDS`, `MAX_BACKENDS` (defaults from `policy.yaml`)
- `COOLDOWN_SECONDS`
- `FORECAST_STALE_SECONDS`

## Status
Shipped — T1.3 (commit `a3e65b0`). Forecast-driven scaling + cooldown + reactive fallback all live.

## See also
- Feature manifest: `docs/features/forecast-autoscale.md` (pending)
- Tests: `tests/integration/test_autoscaler.py`, `tests/integration/test_autoscaler_decisions.py`
