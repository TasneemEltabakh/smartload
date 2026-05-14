# lb_adapters

The interface that decouples SmartLoad's decision plane from any specific load balancer.

## Why this exists

If SmartLoad's decision-plane code calls `nginx -s reload` directly, the entire stack becomes NGINX-only. The first time an Envoy or HAProxy user shows up, the codebase needs a rewrite. The adapter pattern stops that.

## Plugin per folder

One folder per adapter. Each folder is self-contained — implementation, README, tests. Never a flat dump (anti-KEDA-scalers).

| Folder | Status | Behavior |
|---|---|---|
| `nginx/` | working in v1 | rewrites upstream block + signals reload |
| `envoy/` | stub | `NotImplementedError` until issue is filed |
| `haproxy/` | stub | same |
| `alb/` | stub | same |

## Conformance

Every adapter must pass `tests/conformance/lb_adapter/` — the same test suite proves Envoy and HAProxy are interchangeable with NGINX.

## Selection

`LB_ADAPTER` env var on the consumer service (e.g. the future T2.1 sidecar). Defaults to `nginx`.
