# resource-collector

Host-resource shipper. Polls the Docker Engine stats API for every SmartLoad container and emits OTLP/HTTP-JSON gauges (CPU %, memory used / limit / %) to the OTel Collector, on the same pipeline as `lb-otel-shipper`.

## Role
- Lists Compose-project containers every `POLL_INTERVAL_S` (re-listed each cycle so autoscaler-provisioned backends appear automatically).
- Reads one `container.stats(stream=False)` sample per container (carries both `cpu_stats` and `precpu_stats`, so a single call yields the CPU delta).
- Emits four flat gauges per container in the telemetry long format (`time, service, instance, metric_name, value`): `cpu_percent` (100 = one full core), `memory_used_bytes`, `memory_limit_bytes`, `memory_percent`.
- Keys `instance` to match the request-metric instance column (`<container>:8080` for test-backends, `<container>` otherwise) so the operator UI can join CPU/memory with rps/latency per backend.

## Why it exists
NGINX (and so `lb-otel-shipper`) only sees request signals — it cannot report a backend's CPU/memory; those live in the Docker cgroup accounting. The autoscaler holds a Docker client but its remit is scaling, so folding metric collection in would entangle the control loop's cadence with a telemetry concern. A dedicated daemon keeps the same single-responsibility shape as `lb-otel-shipper`. Every hop is fire-and-forget: any stats/POST error logs and drops, never raising into the poll loop.

## Env vars
- `OTEL_ENDPOINT` — OTel Collector OTLP/HTTP endpoint
- `COMPOSE_PROJECT` — Compose project to filter containers (default `smartload`)
- `POLL_INTERVAL_S` — stats poll cadence

## Status
Shipped — #168 (v1.0.7bb). Zero schema change; reuses the existing telemetry `metrics` long format.

## See also
- SOT §8.1.1, §8.3
- Surfaced via telemetry `GET /api/v1/metrics/resources`
