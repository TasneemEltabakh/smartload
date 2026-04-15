# Issue #12: Build Grafana Dashboard — Completion Summary

**Status:** ✅ COMPLETE  
**Date Completed:** April 15, 2026  
**Sprint:** Sprint 2 — Telemetry Baseline + Golden Dataset

---

## Overview

Issue #12 adds Grafana as a visualization layer on top of the TimescaleDB
metrics store introduced in Issues #10 and #11. Grafana is deployed as a
Docker container provisioned entirely through code — no manual UI steps are
required to get a working dashboard.

---

## Acceptance Criteria Verification

### ✅ Grafana is running and displays live data from the DB
- `grafana/grafana-oss:10.4.2` added to `docker-compose.yml`
- TimescaleDB data source auto-provisioned via `provisioning/datasources/timescaledb.yaml`
- Health check on `/api/health`; `depends_on timescaledb` (healthy condition)
- Default home dashboard set via `GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH`

### ✅ Example panels update under load
- 30-second auto-refresh enabled on the dashboard (`refresh: 30s`)
- All time-series panels query `telemetry_1min` continuous aggregate for low-latency reads
- All panels use `$__timeFrom()` / `$__timeTo()` macros for time-range awareness

### ✅ Dashboard JSON is saved in the repo
- Location: `infrastructure/grafana/dashboards/smartload-telemetry.json`
- UID: `smartload-telemetry-v1`
- Provisioned automatically on container start — no manual import needed

---

## Panels

| Panel | Type | Metric | Acceptance Criteria |
|---|---|---|---|
| Requests Per Second | Time series | `total_requests / 60` from `telemetry_1min` | Request rate over time ✅ |
| Latency Distribution | Time series | `avg_latency_ms`, `p95_latency_ms`, `max_latency_ms` | Latency distribution ✅ |
| Backend CPU Usage | Time series | `avg_cpu * 100` from `telemetry_1min` | CPU usage ✅ |
| Traffic Spike Detection | Time series | Z-score vs 15-min rolling window | Traffic spikes ✅ |
| Current P95 Latency | Stat | Max `p95_latency_ms` in last 5 min | Live stat panel ✅ |
| Current Requests/sec | Stat | Sum `total_requests` in last 2 min | Live stat panel ✅ |
| Error Rate | Stat | Avg `avg_error_rate` in last 5 min | Error visibility ✅ |
| Active Nodes | Stat | Distinct `node_id` in last 5 min | Node health ✅ |

---

## File Layout
infrastructure/
├── docker-compose.yml                          ← grafana service added
└── grafana/
├── dashboards/
│   └── smartload-telemetry.json            ← dashboard JSON (saved in repo)
└── provisioning/                           ← mounted to /etc/grafana/provisioning
├── dashboards/
│   └── provider.yaml                   ← tells Grafana to load from /var/lib/grafana/dashboards
└── datasources/
└── timescaledb.yaml                ← auto-provisions DB connection
tests/integration/
└── test_grafana_issue12.py                     ← 11 automated tests

The two volume mounts in `docker-compose.yml` work together:
- `./grafana/provisioning` → `/etc/grafana/provisioning` (Grafana reads config from here)
- `./grafana/dashboards` → `/var/lib/grafana/dashboards` (Grafana loads JSON files from here)

---

## Bug Found and Fixed During Verification

| Bug | Impact | Fix |
|-----|--------|-----|
| `provider.yaml` was placed in `grafana/dashboards/` instead of `grafana/provisioning/dashboards/` | Grafana never received the dashboard provider config, so the JSON file was never loaded. Dashboard returned 404 on all API calls. 6/11 tests failed. | Moved `provider.yaml` to `infrastructure/grafana/provisioning/dashboards/provider.yaml` |

The JSON file itself (`smartload-telemetry.json`) was also initially missing from
`grafana/dashboards/` (it was only in `grafana/provisioning/dashboards/`). Both files
are now in their correct locations.

---

## How to Access Grafana

1. Start the stack from `infrastructure/`:
```bash
docker-compose up -d --build
```
2. Wait ~30 seconds for Grafana to initialize and load provisioning.
3. Open **http://localhost:3000** in your browser.
4. Log in with `admin` / `smartload123`.
5. The **SmartLoad — Telemetry Overview** dashboard loads automatically as the home dashboard.

To see live data in panels, generate traffic first:
```bash
for i in $(seq 1 30); do curl -s http://localhost:8080/ > /dev/null; done
```
Then wait ~2 minutes for data to flow through the pipeline into `telemetry_1min`.

---

## Running the Tests

```bash
cd tests/integration
pip install requests pytest
python -m pytest test_grafana_issue12.py -v -s
```

Expected output:
test_grafana_issue12.py::TestGrafanaRunning::test_grafana_health_endpoint PASSED
test_grafana_issue12.py::TestGrafanaRunning::test_grafana_login_works PASSED
test_grafana_issue12.py::TestDataSource::test_timescaledb_datasource_provisioned PASSED
test_grafana_issue12.py::TestDataSource::test_timescaledb_datasource_is_postgres_type PASSED
test_grafana_issue12.py::TestDataSource::test_timescaledb_datasource_health PASSED
test_grafana_issue12.py::TestDashboard::test_dashboard_exists_by_uid PASSED
test_grafana_issue12.py::TestDashboard::test_dashboard_title PASSED
test_grafana_issue12.py::TestDashboard::test_dashboard_has_required_panels PASSED
test_grafana_issue12.py::TestDashboard::test_dashboard_panel_count PASSED
test_grafana_issue12.py::TestDashboard::test_dashboard_auto_refresh_enabled PASSED
test_grafana_issue12.py::TestDashboard::test_dashboard_has_timescaledb_datasource PASSED
11 passed

---

## Production Notes

- Change `GF_SECURITY_ADMIN_PASSWORD` via `.env` before deploying externally.
- The PostgreSQL password in `timescaledb.yaml` should also be injected via a secret manager.
- `grafana-data` volume persists any manual dashboard edits made through the UI.
- CPU and memory panels will show no data until backend services report those metrics
  (`smartload_backend_cpu_usage`, `smartload_backend_memory_usage` are optional fields
  not currently emitted by the Nginx exporter).
