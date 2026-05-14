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
