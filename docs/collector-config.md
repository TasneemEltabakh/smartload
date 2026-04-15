# OTel Collector Configuration Guide

## Overview
The OpenTelemetry Collector is configured to:
1. **Receive** metrics from Nginx exporter (Prometheus scrape)
2. **Process** metrics through batch and enrichment pipelines
3. **Export** to TimescaleDB via HTTP ingester

## Configuration File Location
`infrastructure/otel-collector/collector-config.yaml`

## Configuration Sections

### Receivers
- **prometheus**: Scrapes `:9113/metrics` from nginx-metrics-exporter every 15s using `metric_relabel_configs` to keep only `smartload_*` metrics
- **otlp**: Accepts direct OTLP/gRPC (4317) and HTTP (4318) pushes (for future use)

### Processors
1. **batch**: Accumulates 100 metrics or waits 10s before forwarding
2. **attributes**: Enriches with required `source` and `environment`
3. **resourcedetection**: Auto-detects system/docker info

### Exporters
- **prometheusremotewrite**: Forwards metrics to timescaledb-ingester via
  Prometheus remote_write protocol (snappy-compressed protobuf) at
  `http://timescaledb-ingester:5555/api/v1/write`
- **logging**: Debug exporter (logs each metric batch)

### Pipelines
- `metrics/prometheus`: Main pipeline (Prometheus scrape → Batch → DB)
- `metrics/otlp`: Alternative pipeline for direct OTLP pushes

## Important Configuration Note

Metric-name filtering **must** use `metric_relabel_configs`, not `relabel_configs`.
`relabel_configs` applies to scrape targets (URLs, ports) and cannot see metric names.
`metric_relabel_configs` applies after scraping and filters by `__name__`.

```yaml
# CORRECT
metric_relabel_configs:
  - source_labels: [__name__]
    regex: "smartload_.*"
    action: keep

# WRONG — silently does nothing for metric filtering
relabel_configs:
  - source_labels: [__name__]
    regex: "smartload_.*"
    action: keep
```

## Histogram Metrics

The Nginx exporter emits histogram metrics which Prometheus expands into three series per metric:
- `smartload_request_latency_ms_bucket` (one per bucket boundary)
- `smartload_request_latency_ms_count`
- `smartload_request_latency_ms_sum`

The ingester's `name_map` handles all three variants and maps them to the correct
database column. Any new histogram metric added to the exporter must have all three
`_bucket`, `_count`, and `_sum` variants added to the ingester's `name_map` in
`infrastructure/timescaledb-ingester/app.py`.

## Customization

### Change Scrape Interval
Edit `scrape_interval` in `collector-config.yaml`:
```yaml
receivers:
  prometheus:
    config:
      scrape_configs:
        - job_name: "smartload-nginx"
          scrape_interval: 30s  # Change from 15s to 30s
```

### Add More Scrape Targets
```yaml
receivers:
  prometheus:
    config:
      scrape_configs:
        - job_name: "smartload-nginx"
          static_configs:
            - targets:
                - "nginx-metrics-exporter:9113"
                - "other-exporter:9114"  # Add here
```

### Change Export Endpoint
Edit `exporters.prometheusremotewrite.endpoint` in `collector-config.yaml`:
```yaml
exporters:
  prometheusremotewrite:
    endpoint: "http://my-custom-ingester:8000/api/v1/write"
    tls:
      insecure: true
```

## Troubleshooting

### Collector won't start
Check logs:
```bash
docker-compose logs otel-collector
```

Common errors:
- `port already in use`: Change port in docker-compose.yml
- `connection refused`: Ensure timescaledb-ingester is running

### Metrics not flowing to DB
1. Check collector is receiving metrics:
```bash
curl http://localhost:8888/metrics | grep otelcol_receiver_accepted
```
2. Check ingester stats (total_stored should be > 0 after traffic):
```bash
curl http://localhost:5555/stats
```
3. Verify exporter is healthy and emitting metrics:
```bash
curl http://localhost:9113/health
curl http://localhost:9113/metrics
```
4. Check ingester logs for validation rejections:
```bash
docker-compose logs timescaledb-ingester --tail=30
```

### total_stored is 0 but total_accepted > 0
This means metrics pass validation but fail to insert. Check:
```bash
docker-compose logs timescaledb-ingester | grep "Insert failed"
docker-compose logs timescaledb --tail=20
```
Most likely cause: schema not initialized. Run the integration tests to confirm:
```bash
cd tests/integration && python -m pytest test_collector_e2e.py -v -s
```

### High latency
Increase batch size or timeout:
```yaml
processors:
  batch:
    send_batch_size: 500  # Increase from 100
    timeout: 30s          # Increase from 10s
```

## Monitoring

Check collector internal metrics:
```bash
curl http://localhost:8888/metrics | grep -E "otelcol_receiver|otelcol_exporter"
```

Check pipeline throughput (accepted vs dropped):
```bash
curl http://localhost:8888/metrics | grep "accepted\|dropped\|refused"
```

Check ingester statistics:
```bash
curl http://localhost:5555/stats
```