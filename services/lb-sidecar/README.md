# lb-sidecar

Subscribes to `smartload.routing` and `smartload.anomaly` (Redis pub/sub) and
dynamically rewrites NGINX's upstream configuration, enabling PPO-driven weighted
routing and automatic backend exclusion on anomaly detection.

## Role

Closes the T2.1 loop: rl-engine publishes `RoutingRecommendation` envelopes
(per-backend scores); anomaly-detector publishes `AnomalyEvent` (health flags).
This service translates those signals into NGINX upstream config rewrites and
signals NGINX to reload, all without blocking the data plane.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `LB_SIDECAR_RUNLOOP_ENABLED` | `false` | Enable message-drain loop |
| `REDIS_URL` | `redis://redis:6379` | Redis connection string |
| `NGINX_CONTAINER` | `smartload-load-balancer-1` | Docker container name for `nginx -s reload` |
| `NGINX_CONF_PATH` | `/nginx-conf/upstream.conf` | Path to the include file sidecar writes |
| `LB_ADAPTER` | `nginx` | Adapter selection (future: `haproxy`, `envoy`) |
| `POLL_INTERVAL_SECONDS` | `5` | Redis message drain interval (seconds) |
| `ALL_BACKENDS` | `smartload-test-backend-{1..5}:8080` | Comma-separated seed backend list |
| `PORT` | `8087` | Flask listen port |

## HTTP endpoints

- `GET /health` — liveness. Adds `sidecar_ready`, `last_routing_age_seconds`,
  `excluded_backends` when `LB_SIDECAR_RUNLOOP_ENABLED=true`.
- `GET /api/v1/lb/state` — current adapter state (weights + exclusions).
- `POST /api/v1/lb/weights` — operator weight override (JSON body).

## Activation

```bash
LB_SIDECAR_RUNLOOP_ENABLED=true \
RL_RUNLOOP_ENABLED=true \
RL_POLICY=ppo \
RL_MODE=active \
docker compose up -d --build lb-sidecar rl-engine
```

## Backend ID translation

NGINX logs `$upstream_addr` (resolved IP:port) which lb-otel-shipper stores
as the `instance` label in the metrics table. rl-engine therefore publishes
IP-based `backend_id` values in `server_rankings`. The sidecar's
`BackendRegistry` class uses the Docker SDK to map IP:port → container_name:port
at startup and on any unmapped lookup.

## Upgrade path

To add a new adapter (HAProxy, Envoy), implement `LoadBalancerAdapter` from
`services/shared/lb_adapters/base.py` and set `LB_ADAPTER` accordingly.
