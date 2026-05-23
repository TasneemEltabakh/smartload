# LB Sidecar — Dynamic Upstream Rewriting

> **T2.1 — shipped 2026-05-23.** Closes the PPO routing loop: rl-engine publishes
> `RoutingRecommendation` envelopes on `smartload.routing`; the lb-sidecar subscribes,
> translates IP-based backend IDs to container hostnames, and atomically rewrites
> NGINX's upstream config, enabling weighted routing and automatic backend exclusion.

## What this feature delivers

Before T2.1, the NGINX upstream block was static. rl-engine and anomaly-detector published
routing and health signals but nothing consumed them for traffic shaping.

T2.1 closes the loop:

- **PPO-driven weighted routing** — when `RL_MODE=active` and `LB_SIDECAR_RUNLOOP_ENABLED=true`,
  active `RoutingRecommendation` envelopes from the rl-engine are translated into NGINX
  `weight=N` directives and applied within one poll cycle (~5s).
- **Anomaly-driven exclusion** — `AnomalyEvent` with `status=unhealthy` adds
  `server addr down;` to the upstream block for the affected backend. `healthy`/`degraded`
  restores it.
- **Safe-mode fallback** — a `PolicyUpdate` with `safe_mode=true` reverts to equal weights
  while preserving any existing exclusions.
- **Operator weight override** — `POST /api/v1/lb/weights` lets operators force weights from
  the UI or API without waiting for the next rl-engine cycle.

## Architecture

```
rl-engine  ──[smartload.routing]──► lb-sidecar ──► /nginx-conf/upstream.conf ──► NGINX reload
anomaly-detector ─[smartload.anomaly]──►|
policy-manager ──[smartload.policy] ──►|
```

The sidecar shares a Docker volume (`nginx-conf`) with the load-balancer. It writes
`upstream.conf` atomically (tmp+rename) then signals NGINX via `docker exec nginx -s reload`
using the mounted Docker socket.

### Backend ID translation

NGINX logs `$upstream_addr` (resolved IP:port) which lb-otel-shipper stores as the `instance`
label. rl-engine's `server_rankings` therefore carry IP-based backend IDs, not container
hostnames. The `BackendRegistry` class uses the Docker SDK to maintain a live
`{ip:port → container_name:port}` mapping.

## Customer surfaces

| Surface | Detail |
|---|---|
| HTTP | `GET /api/v1/lb/state` — current weights + exclusions · `POST /api/v1/lb/weights` — operator override (port 8087) |
| BFF | `GET /api/ui/lb/state` · `POST /api/ui/lb/weights` proxy to lb-sidecar |
| UI | Actions page: "Force route weights" form (previously disabled placeholder, now live) |
| Redis | Subscriber on `smartload.routing` + `smartload.anomaly` + `smartload.policy` |

## Slice checklist compliance

| Layer | Artifact | Status |
|---|---|---|
| Service code | `services/lb-sidecar/` | ✓ |
| Shared adapter | `services/shared/lb_adapters/nginx/__init__.py` | ✓ |
| Envelope contract | `docs/redis-channels.md` subscriber lists updated | ✓ |
| HTTP contract | `GET /api/v1/lb/state` + `POST /api/v1/lb/weights` in openapi yaml | ✓ |
| Unit tests | `tests/unit/lb-sidecar/` (runloop + NginxAdapter) | ✓ |
| Conformance suite | `tests/conformance/lb_adapter/test_conformance.py` | ✓ |
| E2E test | `tests/e2e/lb-sidecar/test_lb_sidecar.py` | ✓ |
| Runnable scenario | `examples/scenarios/lb-sidecar/lb_sidecar_walk.py` | ✓ |
| UI | Actions.tsx "Force route weights" form enabled | ✓ |
| BFF | `POST /api/ui/lb/weights` + `GET /api/ui/lb/state` | ✓ |
| docker-compose | `lb-sidecar` service + `nginx-conf` volume | ✓ |
| CI matrix | `lb-sidecar` added to `build-services` | ✓ |
| SOT alignment | §8.1, §18, §22, §25.9 | pending (docs commit) |

## Activation

```bash
LB_SIDECAR_RUNLOOP_ENABLED=true \
RL_RUNLOOP_ENABLED=true \
RL_POLICY=ppo \
RL_MODE=active \
docker compose up -d --build lb-sidecar rl-engine
```

Verify:
```bash
curl http://localhost:8087/health   # sidecar_ready: true
curl http://localhost:8087/api/v1/lb/state  # current weights
```

## Key env vars

| Variable | Default | Note |
|---|---|---|
| `LB_SIDECAR_RUNLOOP_ENABLED` | `false` | Must be `true` for dynamic routing |
| `NGINX_CONTAINER` | `smartload-load-balancer-1` | Docker container name for reload |
| `NGINX_CONF_PATH` | `/nginx-conf/upstream.conf` | Shared volume path |
| `ALL_BACKENDS` | `smartload-test-backend-{1..5}:8080` | Seed backend list |

## Design decisions

- **Include-file strategy** over full nginx.conf rewrite — only one file changes per cycle;
  NGINX's existing config is untouched.
- **Docker exec over HTTP API** — NGINX open-source has no reload API; exec is how the
  autoscaler also drives Docker, so the pattern is established.
- **Atomic write** — `os.replace(tmp, final)` is atomic on POSIX; NGINX sees a complete
  file on the next read-after-exec.
- **BackendRegistry** — keeps a live IP→name map; refreshes on any unmapped lookup so new
  backends are picked up without a restart.
- **Weight floor at 1** — NGINX rejects `weight=0`; any zero score is promoted to 1.
