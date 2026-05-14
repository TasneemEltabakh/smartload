# load-balancer

The NGINX load balancer that fronts the `test-backend` pool. This is the data-plane entry point; all client traffic terminates here and is routed to backends according to the current upstream configuration.

## Role
- Routes incoming HTTP traffic to the `test-backend` pool
- Emits access logs consumed by `lb-otel-shipper`
- Upstream weights are dynamic — future T2.1 sidecar will rewrite them based on Redis signals from anomaly-detector and rl-engine

## Today
Static round-robin upstream block. Sidecar that consumes Redis signals lives in `lb-otel-shipper` (T1.2) and the in-flight T2.1 work.

## See also
- LB adapter interface: `services/shared/lb_adapters/`
- SOT §8.1
