# Issue #10: Deploy Time-Series Metrics Database — Completion Summary

**Status:** ✅ COMPLETE  
**Date Completed:** April 15, 2026  
**Sprint:** Sprint 2 — Telemetry Baseline + Golden Dataset  
**Implementation Approach:** TimescaleDB (PostgreSQL extension) in Docker + custom ingester

---

## Architecture
nginx-metrics-exporter:9113
│  (Prometheus scrape every 15s)
▼
otel-collector:4317/4318
│  (Prometheus remote_write — snappy protobuf)
▼
timescaledb-ingester:5555
│  (validated INSERT via psycopg2 connection pool)
▼
timescaledb:5432
├── smartload.telemetry_metrics       (hypertable, 7-day retention)
├── smartload.telemetry_1min          (continuous aggregate, 30-day retention)
├── smartload.telemetry_hourly        (continuous aggregate, 60-day retention)
├── smartload.lb_request_latencies    (view — Issue #10 explicit AC)
├── smartload.telemetry_validation_failed  (plain table, rejected records)
└── smartload.backend_nodes           (node registry)

---

## Acceptance Criteria Verification

### ✅ A running TimescaleDB instance is available
- Image: `timescale/timescaledb:2.14.2-pg16`
- Container: `infrastructure-timescaledb-1`
- Port: `5432` (mapped to host)
- Healthcheck: `pg_isready -U postgres` (10s interval, 5 retries)
- Persistent volume: `timescaledb-data` (survives container restarts)
- Verified: `docker compose up -d timescaledb` → healthcheck passes within ~30s

### ✅ The database has at least one table to store LB request latencies
- **Primary table:** `telemetry_metrics` hypertable (all raw metrics, time-partitioned)
- **Dedicated view:** `lb_request_latencies` — exposes latency columns only, satisfies this criterion explicitly
- **Continuous aggregates:** `telemetry_1min` (30-day), `telemetry_hourly` (60-day)
- Query example:
```sql
SELECT time, service_name, latency_ms
FROM lb_request_latencies
WHERE time > NOW() - INTERVAL '5 minutes'
ORDER BY time DESC;
```

### ✅ A test insert from OTel Collector succeeds
- Automated via: `tests/integration/test_timescaledb_issue10.py`
- Tests both direct psycopg2 insert AND HTTP POST to ingester (`/metrics` endpoint)
- Also manually verifiable with `infrastructure/timescaledb/verify-schema.sh`
- Sample manual insert:
```bash
psql postgresql://postgres:postgres123@localhost:5432/smartload -c "
  INSERT INTO telemetry_metrics (
    time, service_name, instance_id, node_id,
    smartload_request_latency_ms, smartload_request_count, smartload_error_rate,
    source, environment
  ) VALUES (
    NOW(), 'nginx-lb', 'manual-test', 'a0000000-0000-0000-0000-000000000001'::uuid,
    45.2, 10, 0.01, 'real', 'development'
  );
"
```

### ✅ Schema (SQL file) is checked in
- Location: `infrastructure/timescaledb/init-schema.sql`
- Auto-applied on first container start via `docker-entrypoint-initdb.d/`
- Idempotent: all `CREATE` statements use `IF NOT EXISTS`

---

## Files Delivered

| File | Purpose |
|------|---------|
| `infrastructure/docker-compose.yml` | TimescaleDB service definition |
| `infrastructure/timescaledb/init-schema.sql` | Full schema: hypertable, aggregates, views, retention |
| `infrastructure/timescaledb/init-db.sh` | Creates `smartload` DB then loads schema |
| `infrastructure/timescaledb/verify-schema.sh` | CLI acceptance test script (psql + curl) |
| `infrastructure/timescaledb-ingester/app.py` | HTTP ingestion service with validation |
| `infrastructure/timescaledb-ingester/Dockerfile` | Ingester container (Python 3.11-slim) |
| `infrastructure/timescaledb-ingester/requirements.txt` | psycopg2-binary, python-snappy |
| `tests/integration/test_timescaledb_issue10.py` | Pytest suite — all 4 acceptance criteria, 14 tests |
| `tests/integration/test_collector_e2e.py` | E2E pipeline tests including DB row verification |
| `docs/contracts/notes/ISSUE_10_COMPLETION_SUMMARY.md` | This document |

---

## Schema Quick Reference

### telemetry_metrics (hypertable)
| Column | Type | Notes |
|--------|------|-------|
| `time` | TIMESTAMPTZ | Partition key (1-day chunks) |
| `service_name` | TEXT | e.g. `nginx-lb` |
| `instance_id` | TEXT | e.g. `nginx-001` |
| `node_id` | UUID | Unique node identifier |
| `smartload_request_latency_ms` | FLOAT8 | End-to-end LB latency |
| `smartload_request_count` | INT8 | Requests in scrape window |
| `smartload_error_rate` | FLOAT8 | 0.0–1.0 ratio |
| `smartload_error_count_total` | INT8 | Cumulative error count |
| `smartload_backend_latency_ms` | FLOAT8 | Upstream response time |
| `smartload_routing_backend_requests_total` | INT8 | Per-backend request count |
| `smartload_backend_cpu_usage` | FLOAT8 | Optional, 0.0–1.0 |
| `smartload_backend_memory_usage` | FLOAT8 | Optional, 0.0–1.0 |
| `source` | TEXT | `real` / `synthetic` / etc. |
| `environment` | TEXT | `development` / `production` |
| `attributes` | JSONB | Flexible sidecar metadata |

### Retention Policy
| Object | Type | Retention |
|--------|------|-----------|
| `telemetry_metrics` | hypertable | 7 days |
| `telemetry_1min` | continuous aggregate | 30 days |
| `telemetry_hourly` | continuous aggregate | 60 days |
| `telemetry_validation_failed` | plain table | manual cleanup only* |

> *`telemetry_validation_failed` is a plain PostgreSQL table, not a hypertable.
> TimescaleDB retention policies only apply to hypertables and continuous aggregates,
> so automatic retention is not available for this table.

---

## Bugs Found and Fixed During Verification

| Bug | File | Impact | Fix |
|-----|------|--------|-----|
| `add_retention_policy('telemetry_validation_failed')` called on plain table | `init-schema.sql` | ERROR aborts script, `telemetry_hourly` retention policy never runs | Removed invalid call |
| `telemetry_hourly` retention policy missing as a result | `init-schema.sql` | Only 2 of 3 expected retention policies created on fresh deploy | Fixed by removing the blocking error above |
| `add_continuous_aggregate_policy` window too small for `telemetry_1min` | `init-schema.sql` | `ERROR: policy refresh window too small` on fresh deploy | Changed `start_offset` from `2 minutes` to `10 minutes`, `end_offset` from `1 minute` to `2 minutes` |
| `test_nginx_exporter_health` checks for `smartload_request_count_total` before any traffic | `test_collector_e2e.py` | Test fails on fresh stack with no prior traffic | Added 5 warm-up requests + 3s sleep before assertion |
| `test_generate_traffic_and_verify_in_db` waits only 15s for pipeline | `test_collector_e2e.py` | DB count = 0 on fresh stack (pipeline needs ~35s: scrape + batch + insert) | Increased sleep from 15s to 35s |

---

## How to Start (Clean Deploy)

```bash
cd infrastructure

# Full clean start (wipes old volume, re-runs schema from scratch)
docker-compose down -v
docker-compose up -d --build

# Wait ~30 seconds for TimescaleDB to initialize and schema to load
# Then verify schema
docker cp ./timescaledb/verify-schema.sh infrastructure-timescaledb-1:/tmp/verify-schema.sh
docker exec -it infrastructure-timescaledb-1 bash /tmp/verify-schema.sh

# Run Issue #10 specific tests (14 tests)
cd ..
pip install "psycopg2-binary>=2.9.9" requests pytest
pytest tests/integration/test_timescaledb_issue10.py -v

# Run full E2E pipeline tests (6 tests, requires traffic)
pytest tests/integration/test_collector_e2e.py -v -s
```

Expected results:
- `test_timescaledb_issue10.py` → **14 passed**
- `test_collector_e2e.py` → **6 passed**

---

## Dependencies
- **Depends on:** Docker environment, `smartload` bridge network
- **Required by:** Issue #11 (OTel Collector config), Issue #12 (Grafana dashboard), Issue #14 (Golden Dataset)
- **Schema contract:** All metric names follow `telemetry-v1.md` (Issue #51)