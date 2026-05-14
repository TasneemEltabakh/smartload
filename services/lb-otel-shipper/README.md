# lb-otel-shipper

Log-based OTLP shipper that runs alongside NGINX. Reads NGINX's access logs, parses per-request fields, and ships them to the OTel Collector as OTLP/HTTP-JSON metrics. Preserves per-request fidelity (no in-flight aggregation).

## Role
- Tails NGINX access logs
- Parses `request_time`, `upstream_addr`, `status`, request size, etc.
- Builds OTLP metric envelopes with proper instance + service labels
- POSTs to the OTel Collector

## Why it exists
NGINX's first-party OTel support is incomplete; a sidecar shipper is the most reliable path while keeping per-request fidelity.

## Env vars
- `OTEL_COLLECTOR_URL`
- `NGINX_ACCESS_LOG_PATH`

## Status
Shipped — T1.2 (commit `c3f1846`).

## See also
- SOT §8.1, §8.1.1
- Tests: `tests/integration/test_lb_otel_shipper.py`
