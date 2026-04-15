# Issue #11: Configure Telemetry Pipeline (Collector) — Completion Summary

**Status:** ✅ COMPLETE  
**Date Completed:** April 15, 2026  
**Sprint:** Sprint 2 — Telemetry Baseline + Golden Dataset  
**Implementation Approach:** OpenTelemetry Collector + Prometheus remote_write + TimescaleDB

---

## Overview

Issue #11 implements a complete end-to-end telemetry pipeline that collects metrics
from the Nginx load balancer and stores them in TimescaleDB. The pipeline provides
observability across the SmartLoad system with metrics collection, validation, and
multi-tier aggregation.

**Acceptance Criteria:** ✅ All met and verified by automated tests

---

## Acceptance Criteria Verification

### ✅ Metrics from the LB appear in TimescaleDB
- Nginx exporter emits OTel metrics in Prometheus format at `:9113/metrics`
- OTel Collector scrapes every 15 seconds and forwards via Prometheus remote_write
- Ingester parses snappy-compressed protobuf and inserts into `telemetry_metrics`
- Verified: `SELECT COUNT(*) FROM telemetry_metrics;` returns > 0 after traffic

### ✅ Generate LB requests and verify rows appear in the DB
- Automated test generates 20 HTTP requests through the load balancer
- Waits 15 seconds for pipeline latency (scrape + batch + insert)
- Verified: rows appear with correct `service_name`, `instance_id`, `node_id`
- Verified: `test_generate_traffic_and_verify_in_db` passes with 30+ rows

### ✅ Collector config file is committed to the repo
- `infrastructure/otel-collector/collector-config.yaml` — committed static YAML
- No templating or generation required
- Comments explain every section

### ✅ Deploy OTel Collector container with proper config
- `infrastructure/otel-collector/Dockerfile` uses `otel/opentelemetry-collector-contrib:0.104.0`
- Config mounted as read-only volume at `/etc/otel-collector-config.yaml`
- Health check on `:13133`, logging driver configured

### ✅ Connect LB to exporter endpoint
- OTel Collector Prometheus receiver targets `nginx-metrics-exporter:9113`
- `metric_relabel_configs` filters to only `smartload_*` metrics

### ✅ Test end-to-end data flow
- 6 automated pytest tests in `tests/integration/test_collector_e2e.py`
- All 6 pass: `6 passed in 16.23s`

### ✅ Document the collector configuration
- `docs/collector-config.md` covers receivers, processors, exporters,
  customization, troubleshooting, and monitoring

---

## Deliverables

### 1. OTel Collector — `infrastructure/otel-collector/`

- **collector-config.yaml**: Prometheus receiver scraping `:9113` every 15s,
  batch processor (100 metrics / 10s), attributes processor adding `source` and
  `environment`, `prometheusremotewrite` exporter to ingester at `:5555/api/v1/write`
- **Dockerfile**: `otel/opentelemetry-collector-contrib:0.104.0`, health check on `:13133`

### 2. TimescaleDB Schema — `infrastructure/timescaledb/`

- **init-schema.sql**: Creates `telemetry_metrics` hypertable, `telemetry_1min`
  and `telemetry_hourly` continuous aggregates, `telemetry_validation_failed` table,
  `backend_nodes` table, indexes, retention and compression policies
- **init-db.sh**: Creates `smartload` database and loads schema on first startup

**Known issue fixed:** The original SQL contained `WITH (timescaledb.compress=ON)`
on the `CREATE TABLE` statement which is not valid syntax in TimescaleDB 2.14.2-pg16.
This line has been removed. Compression is instead enabled via `add_compression_policy()`
which is already present in the file.

### 3. TimescaleDB Ingester — `infrastructure/timescaledb-ingester/`

- **app.py**: Receives Prometheus remote_write (snappy + protobuf), validates against
  telemetry-v1 schema, maps metric names, inserts into TimescaleDB. Exposes `/health`,
  `/stats`, `/api/v1/write`, `/metrics` endpoints.
- **Dockerfile**: Python 3.11-slim, psycopg2-binary, python-snappy, libsnappy-dev
- **requirements.txt**: `psycopg2-binary==2.9.9`, `python-snappy==0.7.3`

**Bug fixed:** The `name_map` in `remote_write_to_records()` now includes histogram
variant suffixes (`_bucket`, `_count`, `_sum`) for both `smartload_request_latency_ms`
and `smartload_backend_latency_ms`. Without these, all histogram time series produced
records with empty `metrics: {}` which were correctly rejected by validation, resulting
in `total_stored: 0` despite `total_accepted > 0`.

**Bug fixed:** `insert_record()` now writes all mapped metric columns including
`smartload_error_count_total`, `smartload_backend_latency_ms`, and
`smartload_routing_backend_requests_total` which were missing from the original INSERT.

### 4. Docker Compose — `infrastructure/docker-compose.yml`

Seven services on the `smartload` bridge network:

| Service | Port | Role |
|---|---|---|
| nginx | 8080 | Load balancer |
| nginx-metrics-exporter | 9113 | Prometheus metrics endpoint |
| test-server | — | Backend server |
| traffic-simulator | 8089 | Load generation |
| timescaledb | 5432 | Time-series database |
| otel-collector | 4317, 4318, 8888, 13133 | Metrics pipeline |
| timescaledb-ingester | 5555 | remote_write receiver |

### 5. Integration Tests — `tests/integration/test_collector_e2e.py`

| Test | What it checks |
|---|---|
| `test_nginx_exporter_health` | Exporter running, emitting `smartload_*` metrics |
| `test_otel_collector_health` | Collector health endpoint responding |
| `test_timescaledb_ingester_health` | Ingester `/health` returns `{"status":"healthy"}` |
| `test_db_schema_exists` | `telemetry_metrics` and `telemetry_1min` exist |
| `test_generate_traffic_and_verify_in_db` | Rows appear in DB after traffic, correct identifiers |
| `test_1min_aggregate_populated` | `telemetry_1min` view has rows |

**Result:** `6 passed in 16.23s` ✅

### 6. Documentation — `docs/collector-config.md`

Covers configuration structure, the `metric_relabel_configs` vs `relabel_configs`
distinction, histogram metric handling, customization, troubleshooting, and monitoring.

---

## Data Flow
HTTP Request
↓
Nginx (port 8080)
│ JSON logs to stdout
↓
Nginx Metrics Exporter (port 9113)
│ OTel SDK → Prometheus text format
│ Exposes: smartload_request_count_total, smartload_request_latency_ms,
│          smartload_error_rate, smartload_backend_latency_ms,
│          smartload_routing_backend_requests_total
↓  ← scraped every 15s
OTel Collector (port 8888)
│ metric_relabel_configs: keep smartload_* only
│ batch: 100 metrics or 10s
│ attributes: source=real, environment=development
│ prometheusremotewrite: snappy-compressed protobuf
↓  → POST /api/v1/write
TimescaleDB Ingester (port 5555)
│ Snappy decompress → protobuf parse
│ name_map: handles _bucket/_count/_sum variants
│ telemetry-v1 validation
│ INSERT with ON CONFLICT DO NOTHING
↓
TimescaleDB (port 5432)
├─ telemetry_metrics      (raw, 7-day retention)
├─ telemetry_1min         (continuous aggregate, 30-day)
└─ telemetry_hourly       (continuous aggregate, 60-day)

---

## Bugs Found and Fixed During Verification

| Bug | File | Impact | Fix |
|---|---|---|---|
| `relabel_configs` used instead of `metric_relabel_configs` | `collector-config.yaml` | Metric filter silently did nothing | Changed to `metric_relabel_configs` |
| Histogram `_bucket`/`_count`/`_sum` not in `name_map` | `timescaledb-ingester/app.py` | All histogram series rejected, `total_stored: 0` | Added all variants to `name_map` |
| 3 metric columns missing from INSERT | `timescaledb-ingester/app.py` | `error_count_total`, `backend_latency_ms`, `routing_backend_requests_total` always NULL | Added to INSERT and values tuple |
| `WITH (timescaledb.compress=ON)` invalid syntax | `timescaledb/init-schema.sql` | Schema init crash, no tables created | Removed; compression via `add_compression_policy()` |

---

## Running the Tests

Requirements: Docker Desktop running, all services up via `docker-compose up -d --build`
from the `infrastructure/` directory.

```bash
cd tests/integration
pip install requests pytest psycopg2-binary
python -m pytest test_collector_e2e.py -v -s
```

Expected output:
test_collector_e2e.py::test_nginx_exporter_health PASSED
test_collector_e2e.py::test_otel_collector_health PASSED
test_collector_e2e.py::test_timescaledb_ingester_health PASSED
test_collector_e2e.py::test_db_schema_exists PASSED
test_collector_e2e.py::test_generate_traffic_and_verify_in_db PASSED
test_collector_e2e.py::test_1min_aggregate_populated PASSED
6 passed

---

## Files Changed

| File | Change |
|---|---|
| `infrastructure/otel-collector/collector-config.yaml` | `relabel_configs` → `metric_relabel_configs` |
| `infrastructure/timescaledb-ingester/app.py` | `name_map` expanded; `insert_record()` INSERT completed |
| `infrastructure/timescaledb/init-schema.sql` | Removed invalid `WITH (timescaledb.compress=ON)` |
| `tests/integration/requirements.txt` | Pin to `psycopg2-binary>=2.9.9` for Python 3.11+ compatibility |
| `docs/collector-config.md` | Updated troubleshooting, added histogram and relabel notes |
| `docs/contracts/notes/ISSUE_11_COMPLETION_SUMMARY.md` | This file |

---

**Issue #11 Status:** ✅ COMPLETE — all 6 automated tests passing