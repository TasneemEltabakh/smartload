# telemetry

OTLP/HTTP-JSON ingestion service. Receives metrics pushed by the OTel Collector, writes them to the `metrics` hypertable, and exposes a read API for downstream consumers (anomaly detector, forecasting, RL engine).

## Role
- Accepts OTLP/HTTP-JSON metric pushes from the collector
- Persists rows to `metrics` (TimescaleDB hypertable)
- Exposes a read API for service / window / metric-name slices
- Maintains observability counters (ingest rate, drop rate, parser errors)

## HTTP endpoints
- `GET /health`
- `POST /v1/metrics` — OTLP/HTTP-JSON ingest endpoint (called by the collector)
- `GET /api/v1/metrics?service=<svc>&window=<duration>` — read API

## Env vars
- `TIMESCALEDB_URL`, `REDIS_URL`
- `PORT` (default `8081`)

## Status
Shipped — T1.1 (commit `faa0fcc`). OTLP ingest + read API + counters all live.

## See also
- SOT §8.3, §11
- Tests: `tests/integration/test_telemetry_ingest.py`, `tests/integration/test_telemetry_parser.py`
