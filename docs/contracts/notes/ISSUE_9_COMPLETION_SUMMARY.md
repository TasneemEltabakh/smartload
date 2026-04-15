# Issue #9 Completion Summary — Instrument Load Balancer with OpenTelemetry

**Status:** ✅ COMPLETE  
**Date:** April 12, 2026  
**Sprint:** Sprint 2 — Telemetry Baseline + Golden Dataset

---

## Overview

Integrated OpenTelemetry metrics into the Nginx load balancer using a Python sidecar exporter that streams Nginx JSON logs via the Docker SDK and exposes metrics via the OTel Python SDK with a Prometheus exporter bridge.

The exporter connects to the nginx container through the Docker socket (`/var/run/docker.sock`) and streams logs in real time using `container.logs(stream=True, follow=True)`. This approach is reliable across all platforms including Docker Desktop on Windows, where shared-volume file reads suffer from OS-level caching that prevents new bytes from being visible to other containers.

---

## Deliverables

| Deliverable | File | Status |
|-------------|------|--------|
| Metrics exporter service | `infrastructure/nginx-metrics-exporter/app.py` | ✅ |
| Exporter Dockerfile | `infrastructure/nginx-metrics-exporter/Dockerfile` | ✅ |
| Exporter dependencies | `infrastructure/nginx-metrics-exporter/requirements.txt` | ✅ |
| Updated Nginx config | `infrastructure/nginx/nginx.conf` | ✅ |
| Updated Docker Compose | `infrastructure/docker-compose.yml` | ✅ |
| Smoke test script | `tests/integration/test_otel_smoke.py` | ✅ |
| Documentation | `docs/instrumentation.md` | ✅ |

---

## Metrics Implemented

All instruments are created via the **OpenTelemetry Python SDK** (`opentelemetry-sdk`) with a `PrometheusMetricReader` bridge — not raw `prometheus_client` — so they are true OTel SDK instruments. All metric names follow the **telemetry-v1 schema** (`smartload.<domain>.<metric>`):

| Metric Name | OTel Instrument | Description |
|-------------|-----------------|-------------|
| `smartload_request_count_total` | Counter | Total requests through load balancer |
| `smartload_request_latency_ms` | Histogram | Request latency in milliseconds |
| `smartload_error_count_total` | Counter | Error responses (4xx/5xx) by status class |
| `smartload_error_rate` | ObservableGauge | Error rate over 60-second sliding window (0.0–1.0) |
| `smartload_routing_backend_requests_total` | Counter | Requests routed per backend |
| `smartload_backend_latency_ms` | Histogram | Upstream backend latency in milliseconds |

---

## Labels Applied

All metrics include these labels per telemetry-v1 schema:

| Label | Example Value | Source |
|-------|---------------|--------|
| `service_name` | `nginx-lb` | Environment variable |
| `instance_id` | `nginx-001` | Environment variable |
| `node_id` | `a0000000-0000-0000-0000-000000000001` | Environment variable |
| `source` | `real` | Environment variable |
| `environment` | `development` | Environment variable |
| `backend` | `172.18.0.2:8080` | Parsed from Nginx logs |
| `method` | `GET` | Parsed from Nginx logs |
| `path` | `/` | Parsed from Nginx logs |

---

## Architecture
┌─────────────┐     ┌─────────────────────┐     ┌────────────┐
│   Client    │────▶│   Nginx (LB)        │────▶│  Backend   │
└─────────────┘     │   Port 80           │     │  Servers   │
│   Logs → stdout     │     └────────────┘
└─────────────────────┘
│ Docker socket
│ (docker logs stream)
┌──────────▼──────────┐
│  Metrics Exporter   │
│  Port 9113          │────▶ Prometheus
└─────────────────────┘

**How it works:**
1. Nginx logs each request in JSON format to stdout (`access_log /dev/stdout smartload_json`)
2. The metrics exporter connects to the Docker daemon via `/var/run/docker.sock`
3. It calls `container.logs(stream=True, follow=True, since=now)` on the nginx container to receive only new log lines in real time
4. Each JSON line is parsed for method, path, status, latency, and upstream latency (including multi-value retry strings from `proxy_next_upstream`)
5. Parsed values are recorded into OTel SDK instruments (Counter, Histogram, ObservableGauge)
6. The `PrometheusMetricReader` bridges OTel instruments to Prometheus wire format on `:9113/metrics`
7. Prometheus scrapes `:9113/metrics` on its configured interval

---

## How to Run

### Prerequisites

- Docker Desktop
- Docker Compose
- Python 3.8+ (for smoke test)

### Start Services

```bash
cd infrastructure
docker-compose down -v          # clean slate
docker-compose up -d --build
```

### Verify Services Running

```bash
docker-compose ps
```

Expected: 4 services all with status "Up"
- `infrastructure-nginx-1`
- `infrastructure-nginx-metrics-exporter-1`
- `infrastructure-test-server-1`
- `infrastructure-traffic-simulator-1`

### Verify Exporter Connected

```bash
docker-compose logs nginx-metrics-exporter
```

Expected:
[INFO] SmartLoad Nginx Metrics Exporter starting
[INFO] Service: nginx-lb | Instance: nginx-001 | Node: a0000000-...
[INFO] Nginx container: infrastructure-nginx-1
[INFO] Metrics port: 9113
[INFO] Metrics server listening on :9113
[INFO] Connected to container: infrastructure-nginx-1

---

## How to Test

### Test 1: Check Nginx Health

```bash
curl http://localhost:8080/health
```

**Expected:**
```json
{"status":"healthy"}
```

### Test 2: Check Metrics Exporter Health

```bash
curl http://localhost:9113/health
```

**Expected:**
```json
{"status": "healthy", "service": "nginx-lb", "instance_id": "nginx-001"}
```

### Test 3: Generate Traffic

**Linux/Mac:**
```bash
for i in {1..20}; do curl -s http://localhost:8080/ > /dev/null; done
for i in {1..5};  do curl -s http://localhost:8080/nonexistent > /dev/null; done
```

**Windows PowerShell:**
```powershell
1..20 | ForEach-Object { Invoke-WebRequest http://localhost:8080/ | Out-Null }
1..5  | ForEach-Object { try { Invoke-WebRequest http://localhost:8080/nonexistent | Out-Null } catch {} }
```

### Test 4: Verify Metrics Recorded

**Linux/Mac:**
```bash
curl -s http://localhost:9113/metrics | grep smartload_request_count_total
```

**Windows PowerShell:**
```powershell
(Invoke-WebRequest http://localhost:9113/metrics).Content | Select-String "smartload_request_count_total"
```

**Expected:**
smartload_request_count_total{environment="development",instance_id="nginx-001",node_id="a0000000-0000-0000-0000-000000000001",service_name="nginx-lb",source="real"} 20.0

### Test 5: Verify Latency Histogram

**Linux/Mac:**
```bash
curl -s http://localhost:9113/metrics | grep smartload_request_latency_ms_count
```

**Windows PowerShell:**
```powershell
(Invoke-WebRequest http://localhost:9113/metrics).Content | Select-String "smartload_request_latency_ms_count"
```

**Expected:** Count matches number of requests sent.

### Test 6: Verify Error Tracking

**Linux/Mac:**
```bash
curl -s http://localhost:9113/metrics | grep smartload_error
```

**Windows PowerShell:**
```powershell
(Invoke-WebRequest http://localhost:9113/metrics).Content | Select-String "smartload_error"
```

**Expected:**
- `smartload_error_count_total{...,status_class="4xx"} 5.0`
- `smartload_error_rate{...}` shows non-zero value

### Test 7: Check Exporter Logs

```bash
docker-compose logs nginx-metrics-exporter
```

**Expected:**
[INFO] Connected to container: infrastructure-nginx-1
[METRIC] GET / -> 200 (2.0ms)
[METRIC] GET / -> 200 (1.0ms)
[METRIC] GET /nonexistent -> 404 (1.0ms)
...

### Test 8: Run Automated Smoke Test

```bash
cd tests/integration
pip install -r requirements.txt
pytest test_otel_smoke.py -v
```

**Expected:**
PASSED test_exporter_health
PASSED test_nginx_health
PASSED test_metrics_endpoint_reachable
PASSED test_required_metrics_present
PASSED test_required_labels_present
PASSED test_request_count_nonzero
PASSED test_error_metrics_recorded
PASSED test_latency_histogram_has_samples
8 passed in ~5s

---

## Quick Validation Checklist

| Check | Command | Expected |
|-------|---------|----------|
| Nginx running | `curl localhost:8080/` | `Hello from <server-id>` |
| Nginx health | `curl localhost:8080/health` | `{"status":"healthy"}` |
| Exporter health | `curl localhost:9113/health` | `{"status":"healthy","service":"nginx-lb","instance_id":"nginx-001"}` |
| Exporter connected | `docker-compose logs nginx-metrics-exporter` | `[INFO] Connected to container: infrastructure-nginx-1` |
| Metrics exist | `curl localhost:9113/metrics` | `smartload_` prefixed lines present |
| Request count > 0 | grep `smartload_request_count_total` | Counter with value > 0 |
| Labels correct | grep `node_id` | `node_id="a0000000-..."` |

---

## Troubleshooting

| Problem | Cause | Solution |
|---------|-------|----------|
| No `[METRIC]` lines in logs | Exporter not receiving Docker log stream | Check `docker-compose logs nginx-metrics-exporter` for connection errors |
| `[WARN] Container not found` | Wrong container name | Verify `NGINX_CONTAINER_NAME` matches `docker-compose ps` output |
| Connection refused on 9113 | Exporter crashed | Check logs; rebuild with `docker-compose up -d --build nginx-metrics-exporter` |
| Metrics show 0 requests | Traffic sent before exporter connected | Send fresh traffic after confirming `[INFO] Connected` in logs |
| Docker socket permission denied | Socket not mounted | Verify `/var/run/docker.sock:/var/run/docker.sock` in `docker-compose.yml` |
| Metrics missing after restart | Old volume state | Run `docker-compose down -v` then `docker-compose up -d --build` |

---

## Stop Services

```bash
docker-compose down
```

To also remove volumes:
```bash
docker-compose down -v
```

---

## Acceptance Criteria Verification

| Criteria | Status | Evidence |
|----------|--------|----------|
| Load balancer emits OTel metrics for every request | ✅ | `smartload_request_count_total` increments per request via OTel Counter |
| Metrics visible in OTel Collector or Prometheus format | ✅ | `curl localhost:9113/metrics` returns Prometheus wire format via OTel SDK bridge |
| Example metrics recorded (request_rate, latency_ms, error_count) | ✅ | All 6 instruments present and populated after traffic |
| Smoke test demonstrates OTel data output | ✅ | `pytest test_otel_smoke.py -v` — 8 passed |
| OTel SDK used (not raw prometheus_client) | ✅ | `MeterProvider` + `PrometheusMetricReader` from `opentelemetry-sdk` |

---

## Files Changed/Created
infrastructure/
├── docker-compose.yml                    # Updated: docker socket mount, removed nginx-logs volume
├── nginx/
│   └── nginx.conf                        # Updated: access_log to /dev/stdout
└── nginx-metrics-exporter/               # NEW DIRECTORY
├── app.py                            # Exporter — OTel SDK + Docker log streaming
├── Dockerfile                        # Container build file
└── requirements.txt                  # opentelemetry-sdk + exporter-prometheus + docker SDK
tests/
└── integration/
├── test_otel_smoke.py                # Smoke test — pytest-compatible, 8 tests
└── requirements.txt                  # requests + pytest
docs/
└── instrumentation.md                    # Documentation

---

## Unblocks

This issue completion unblocks:

- **#11 — Configure Telemetry Pipeline (Collector)**: Collector can now scrape metrics from `:9113`
- **#10 — Deploy Time-Series Metrics Database**: Prometheus can store these metrics
- **#12 — Build Grafana Dashboard**: Dashboard can visualize these metrics

---

## Next Steps

Proceed to **Issue #11 — Configure Telemetry Pipeline (Collector)** to set up the OpenTelemetry Collector that will:
1. Scrape metrics from the exporter at `:9113/metrics`
2. Process and transform metrics
3. Export to Prometheus and/or TimescaleDB