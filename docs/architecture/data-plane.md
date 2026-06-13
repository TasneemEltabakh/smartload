# Data plane

The request path. Everything between "client makes a request" and "client gets a response."

## Path

```
client
  └─► load-balancer (NGINX)
        └─► test-backend pool
              ▲
              │ upstream weights rewritten by:
              │   - rl-engine recommendations (when active)
              │   - anomaly exclusions
              │   - policy safe_mode (forces equal weights)
              │
              └── lb-otel-shipper / future T2.1 sidecar
```

## Components

| Component | Role |
|---|---|
| load-balancer (NGINX) | terminates client traffic, routes to upstream pool |
| lb-otel-shipper | tails NGINX access logs, ships OTLP/HTTP-JSON metrics to the collector |
| (future) T2.1 sidecar | subscribes to control-plane Redis channels, rewrites NGINX upstream config |
| test-backend | the backend pool being routed across; Node.js Express stubs |
| otel-collector + telemetry + TimescaleDB | telemetry persistence pipeline feeding the control plane |

## Adapter abstraction

NGINX is an *adapter*, not a hard dependency. See `lb-adapter.md` and `services/shared/lb_adapters/`. The decision plane speaks to `LoadBalancerAdapter`; the NGINX-specific commands (rewrite upstream, `nginx -s reload`) are encapsulated in `nginx/` and future adapters (Envoy, HAProxy, ALB) drop in beside it.

## Failure semantics

- If `lb-otel-shipper` is down: NGINX keeps routing, telemetry pauses, control plane reacts to stale data. Acceptable for short outages.
- If `load-balancer` is down: hard outage. Out of scope for SmartLoad's own resilience; deployer's responsibility (HA NGINX, ingress retries).

## Own-metrics (Prometheus `/metrics`) — #161

Two observability paths run side by side and answer different questions:

- **Workload telemetry** (the path above): NGINX → lb-otel-shipper → OTel Collector → telemetry → TimescaleDB. *Per-request* data (latency, error_rate, request_count per backend) that the decision plane queries and Grafana renders — what the **traffic** is doing.
- **Service own-metrics** (#161): each service exposes a Prometheus `/metrics` endpoint scraped directly by Prometheus (`infrastructure/prometheus/prometheus.yml`). *Per-process* health — decision rate, cycle duration, publish counts, decision distributions — what the **services** are doing.

The common surface is shared via `services/shared/metrics.py` (`ServiceMetrics(prefix)` + `metrics_response()`), so every instrumented service exposes the same templatable metrics under its own prefix:

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `<svc>_up` | gauge | — | 1 while the process is alive |
| `<svc>_cycle_total` | counter | `outcome` | run-loop cycles (`published` / `idle` / `error` / scale action) |
| `<svc>_cycle_duration_seconds` | histogram | — | run-loop cycle wall time |
| `<svc>_publish_total` | counter | `channel`, `outcome` | envelopes published |
| `<svc>_publish_duration_seconds` | histogram | — | publish wall time |

Prefixes: `anomaly_detector`, `forecasting`, `rl_engine`, `autoscaler`, `policy_manager`, `lb_sidecar`. Plus per-service **decision-distribution** counters:

| Metric | Labels | Service |
|---|---|---|
| `anomaly_detector_isolate_total` | `backend`, `status` | anomaly verdicts published per backend |
| `rl_engine_action_total` | `policy`, `mode` | routing inferences by policy + effective mode (active/shadow) |
| `autoscaler_scale_total` | `direction`, `mechanism` | scale actions actuated |
| `lb_sidecar_message_total` | `channel` | control-bus messages consumed (it's a consumer — `publish_*` stays zero by design) |

Prometheus scrapes each on its existing port at the default `/metrics` path (telemetry stays on `/health` until it grows its own surface). The Overview Grafana dashboard's "Decision-plane publish rate" panel renders `sum(rate(<svc>_publish_total[5m]))` from this surface — independent of the TimescaleDB path.
