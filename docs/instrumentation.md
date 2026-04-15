# SmartLoad Load Balancer Instrumentation

## Overview

The Nginx load balancer is instrumented using a **sidecar metrics exporter** that:

1. Tails Nginx JSON access logs
2. Parses request data (latency, status, backend)
3. Exposes Prometheus metrics on port `9113`

This approach follows the **telemetry-v1 schema** defined in `/docs/contracts/telemetry-v1.md`.

## Architecture
┌─────────────┐     ┌─────────────────────┐     ┌────────────┐
│   Client    │────▶│   Nginx (LB)        │────▶│  Backend   │
└─────────────┘     │   Port 80           │     │  Servers   │
│   Logs → /var/log/  │     └────────────┘
└──────────┬──────────┘
│ shared volume
┌──────────▼──────────┐
│  Metrics Exporter   │
│  Port 9113          │────▶ Prometheus
└─────────────────────┘

## Metrics Exposed

| Metric Name | Type | Description |
|-------------|------|-------------|
| `smartload_request_count_total` | Counter | Total requests through LB |
| `smartload_request_latency_ms` | Histogram | Request latency (ms) |
| `smartload_error_count_total` | Counter | Error responses (4xx/5xx) |
| `smartload_error_rate` | Gauge | Current error rate (0.0-1.0) |
| `smartload_routing_backend_requests_total` | Counter | Requests per backend |
| `smartload_backend_latency_ms` | Histogram | Upstream latency (ms) |

## Labels

All metrics include these labels (per telemetry-v1 schema):

- `service_name`: `nginx-lb`
- `instance_id`: `nginx-001`
- `node_id`: UUID assigned to this LB instance
- `source`: `real` or `synthetic`
- `environment`: `development`, `staging`, `production`

## Running

```bash
# Start all services
cd infrastructure
docker-compose up -d

# Check metrics endpoint
curl http://localhost:9113/metrics

# Run smoke test
cd ../tests/integration
pip install -r requirements.txt
python test_otel_smoke.py
```

## Configuration

Environment variables for the metrics exporter:

| Variable | Default | Description |
|----------|---------|-------------|
| `NGINX_LOG_PATH` | `/var/log/nginx/access.log` | Path to Nginx log file |
| `METRICS_PORT` | `9113` | Prometheus scrape port |
| `SERVICE_NAME` | `nginx-lb` | Service identifier |
| `INSTANCE_ID` | `nginx-001` | Instance identifier |
| `NODE_ID` | `00000000-...` | UUID for this node |
| `SOURCE` | `real` | Data source tag |
| `ENVIRONMENT` | `development` | Environment tag |

## Prometheus Scrape Config

Add to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'smartload-nginx'
    static_configs:
      - targets: ['nginx-metrics-exporter:9113']
    scrape_interval: 15s
```

## Future: OTLP Export

OTLP export can be added by installing `opentelemetry-exporter-otlp` and adding an OTLP exporter alongside Prometheus. See Issue #11 for collector configuration.