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
| `LB_SIDECAR_RUNLOOP_ENABLED` | `true` (since v1.0.7g) | Set to `false` to revert to the Phase-0 stub (no subscribers, `/health` only). The sidecar's `mode != "active"` gate still prevents routing changes from shadow envelopes. |
| `REDIS_URL` | `redis://redis:6379` | Redis connection string |
| `NGINX_CONTAINER` | `smartload-load-balancer-1` | Docker container name for `nginx -s reload` |
| `NGINX_CONF_PATH` | `/nginx-conf/upstream.conf` | Path to the include file sidecar writes |
| `LB_ADAPTER` | `nginx` | Adapter selection (future: `haproxy`, `envoy`) |
| `POLL_INTERVAL_SECONDS` | `5` | Redis message drain interval (seconds) |
| `ALL_BACKENDS` | `smartload-test-backend-{1..5}:8080` | Comma-separated seed backend list |
| `TIMESCALEDB_URL` | `postgresql://postgres:changeme@timescaledb:5432/smartloaddb` | TimescaleDB connection for startup `backend_health` hydration |
| `LB_SIDECAR_HEALTH_HYDRATION_WINDOW_SECONDS` | `300` | Window passed to `BACKEND_HEALTH_QUERY` on startup |
| `PORT` | `8087` | Flask listen port |

## HTTP endpoints

- `GET /health` — liveness. When `LB_SIDECAR_RUNLOOP_ENABLED=true`, adds
  `sidecar_ready`, `last_routing_age_seconds`, `excluded_backends`,
  `policy_safe_mode`, `rl_confidence_threshold`.
- `GET /api/v1/lb/state` — current adapter state (weights + exclusions).
- `POST /api/v1/lb/weights` — operator weight override (JSON body).
- `POST /api/v1/lb/algorithm` — switch NGINX upstream algorithm
  (`round_robin` | `least_conn` | `random`).

## SOT-anchored behaviour (v1.0.7b)

- **Anomaly exclusion is preserved across RL publishes**. When RL ranks
  only the eligible subset, omitted backends still appear in
  `upstream.conf` at a floor weight; previously-excluded backends are
  rendered as `down;` regardless of whether RL named them. SOT §3.4
  line 1756, §16 line 3699.
- **Startup hydrates from `backend_health`** (TimescaleDB). Exclusions
  survive Redis disconnects and sidecar restarts. SOT §30 lines 7635 + 7760.
- **`rl_confidence_threshold` is enforced**. If `max(scores) < threshold`,
  the recommendation is rejected and the previous adapter state stands.
  Threshold 0 disables the gate. SOT §13 line 3128.
- **Shadow envelopes are logged**, never applied. SOT v1.0.6 row line 5703.
- **Redis reconnects** on `ConnectionError` with a short backoff. NGINX
  keeps serving the last applied weights throughout. SOT §8.1 line 2299.
- **SIGTERM/SIGINT** drain the current message and close pubsub cleanly.

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
