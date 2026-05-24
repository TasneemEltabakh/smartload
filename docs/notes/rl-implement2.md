# SmartLoad — Implementation Continuity Document (Part 2)

**Document scope:** Continuation of `rl-implement1.md`. Covers everything from commit
`7211eca` through `dca9403` (2026-05-23 to 2026-05-24): the T2.1 lb-sidecar implementation,
its end-to-end smoke test, and all bugs found and fixed during execution. Part 1 documents
N2.3–N2.5; start there if you need the RL engine details.

**Commits covered:**
- `398ed99` — `feat(lb-sidecar): T2.1 complete`
- `7211eca` — `docs: T2.1 lb-sidecar SOT updates`
- `d37e209` — `fix(lb-sidecar/smoke): T2.1 smoke test PASSED — walk + Dockerfile fixes`
- `dca9403` — `docs: v1.0.5 — T2.1 smoke PASSED`

---

## Table of Contents

1. [T2.1 — What Changed from the Prediction in Part 1](#1-t21--what-changed-from-the-prediction-in-part-1)
2. [T2.1 Architecture Design Decisions](#2-t21-architecture-design-decisions)
3. [Updated Container Inventory](#3-updated-container-inventory)
4. [Updated Redis Channel Table](#4-updated-redis-channel-table)
5. [lb-sidecar Service Structure](#5-lb-sidecar-service-structure)
6. [runloop.py — Pure Logic Layer](#6-runlooppy--pure-logic-layer)
7. [NginxAdapter — LoadBalancerAdapter Implementation](#7-nginxadapter--loadbalanceradapter-implementation)
8. [BackendRegistry — IP-to-Name Bridge](#8-backendregistry--ip-to-name-bridge)
9. [app.py — Flask Layer and Env Vars](#9-apppy--flask-layer-and-env-vars)
10. [nginx-conf Volume Bootstrap Problem](#10-nginx-conf-volume-bootstrap-problem)
11. [docker-compose Changes](#11-docker-compose-changes)
12. [Test Suite Map — lb-sidecar](#12-test-suite-map--lb-sidecar)
13. [Smoke Test Execution](#13-smoke-test-execution)
14. [Bugs Found and Fixed During Smoke Test](#14-bugs-found-and-fixed-during-smoke-test)
15. [Smoke Test Results and Artifacts](#15-smoke-test-results-and-artifacts)
16. [Updated Phase Status](#16-updated-phase-status)
17. [Key Invariants — lb-sidecar Additions](#17-key-invariants--lb-sidecar-additions)

---

## 1. T2.1 — What Changed from the Prediction in Part 1

Part 1 listed T2.1 as Phase 2 pending and described the open design question:

> The NGINX adapter requires a mechanism for live upstream weight changes. Options were:
> NGINX Plus API (commercial), `nginx -s reload` (simple, more latency), OpenResty/Lua
> (complex), or HAProxy runtime API.

**Decision made: `nginx -s reload` via Docker exec.**

Rationale: Simplest implementation, no commercial dependency, compatible with the existing
nginx:1.25-alpine base image. The reload latency (~15–25 s on Docker Desktop for Windows,
< 1 s on Linux) is acceptable for a control-plane action that fires every few seconds at
most. The upstream.conf file is rewritten atomically (tmp + rename) before the reload signal
is sent so NGINX never reads a partial file.

Part 1 also described the `lb_adapters/` stubs as already scaffolded. What was actually
present: empty `__init__.py` files under `services/shared/lb_adapters/{nginx,envoy,haproxy,alb}/`.
Only the NGINX implementation was written for T2.1.

---

## 2. T2.1 Architecture Design Decisions

### Include-file strategy for NGINX

Instead of rewriting `nginx.conf` on every routing update, `nginx.conf` contains a single
include directive:

```nginx
upstream backend_pool {
    include /etc/nginx/conf.d/upstream.conf;
}
```

The sidecar only ever writes to `upstream.conf`. This limits the blast radius of a bad write
to a single file with a known format, and makes the sidecar's output inspectable without
reading nginx.conf.

### Shared Docker volume (`nginx-conf`)

`upstream.conf` lives on a Docker named volume `nginx-conf`:
- mounted at `/etc/nginx/conf.d/` in `load-balancer`
- mounted at `/nginx-conf/` in `lb-sidecar`

This is the only mechanism by which lb-sidecar can write to NGINX's config directory from
outside the container.

### Weight mapping formula

```
nginx_weight = max(1, round(score * 100))
```

`server_rankings` scores are floats in `(0, 1]`. Multiplying by 100 and rounding gives NGINX
integer weights in `[1, 100]`. `max(1, ...)` prevents a weight of 0, which would be a
syntax error in the NGINX upstream block. Excluded backends get `server addr down;` instead.

### Backend ID mismatch bridged by BackendRegistry

rl-engine sources its backend IDs from `$upstream_addr` (NGINX's resolved IP:port), while
NGINX's upstream block uses container hostnames (`smartload-test-backend-N:8080`). The
`BackendRegistry` class queries the Docker SDK to map IPs to container names at startup and
refreshes on any unmapped lookup.

If the walk script or a manual publisher uses container hostnames directly (not IP-based
IDs), the registry passes them through unchanged — it only translates IDs that look like IPs
(`"." in backend_id.split(":")[0]`).

---

## 3. Updated Container Inventory

Two new containers added. The container count went from 14 to 16 (with 5 test-backend
replicas, the running total is 21 containers).

| Container | Port | Role | Run loop default |
|---|---|---|---|
| `lb-sidecar` | 8087 | Dynamic NGINX upstream rewriting | `LB_SIDECAR_RUNLOOP_ENABLED=false` |
| `load-balancer` | 8080 | NGINX reverse proxy (now with nginx-conf volume) | always on |

All other containers are unchanged from Part 1. The `load-balancer` container gained the
`nginx-conf` volume mount; its image also gained the `00-seed-upstream.sh` bootstrap script.

---

## 4. Updated Redis Channel Table

`smartload.routing` and `smartload.anomaly` are no longer "future T2.1" — lb-sidecar is
now a live subscriber.

| Channel | Publisher | Subscribers |
|---|---|---|
| `smartload.policy` | policy-manager | all AI services |
| `smartload.anomaly` | anomaly-detector | **lb-sidecar** |
| `smartload.forecast` | forecasting | autoscaler |
| `smartload.routing` | rl-engine | **lb-sidecar** |
| `smartload.scale` | autoscaler | operator-ui |
| `smartload.policy` | policy-manager | lb-sidecar (safe_mode gate) |

lb-sidecar subscribes to all three channels in a single Redis pub/sub connection.

---

## 5. lb-sidecar Service Structure

```
services/lb-sidecar/
├── app.py              Flask entry point + run loop thread
├── runloop.py          Pure-Python logic (no Flask, no Docker, no Redis)
├── requirements.txt    flask, redis, docker
├── Dockerfile          FROM python:3.11-slim; COPY shared
└── README.md           Service role, env vars, /health contract

services/shared/lb_adapters/
└── nginx/
    └── __init__.py     NginxAdapter(LoadBalancerAdapter)

tests/unit/lb-sidecar/
├── test_runloop.py     BackendRegistry, scores_to_weights, handle_*
└── test_nginx_adapter.py  NginxAdapter with tmp files + mock exec

tests/conformance/lb_adapter/
└── test_conformance.py   Idempotency, state, all-excluded fallback

tests/e2e/lb-sidecar/
├── conftest.py
└── test_lb_sidecar.py

examples/scenarios/lb-sidecar/
└── lb_sidecar_walk.py

docs/features/
└── lb-sidecar.md       Feature manifest
```

The code follows the **identical `app.py` + `runloop.py` split** used by rl-engine,
anomaly-detector, and forecasting. No TimescaleDB connection — lb-sidecar is purely
Redis + Docker SDK + filesystem.

---

## 6. runloop.py — Pure Logic Layer

**File:** `services/lb-sidecar/runloop.py`

### `scores_to_weights(server_rankings)`

```python
def scores_to_weights(server_rankings: list[dict]) -> dict[str, int]:
    result = {}
    for entry in server_rankings:
        backend_id = entry.get("backend_id", "")
        score = float(entry.get("score", 0.0))
        result[backend_id] = max(1, round(score * 100))
    return result
```

Keys are backend IDs (IP:port or hostname:port). Translation to hostnames happens in the
caller via `BackendRegistry.translate()`.

### `handle_routing(payload, registry, adapter, all_backends)`

The shadow gate lives here:

```python
mode = payload.get("mode", "shadow")
if mode != "active":
    return RoutingOutcome(applied=False, mode=mode, weight_count=len(rankings))
```

Any envelope with `mode != "active"` is a no-op. No adapter call, no nginx reload, no file
write. This is the shadow gate — it is enforced in pure Python, not in the adapter.

When `mode == "active"`:
1. `scores_to_weights(rankings)` → raw weights dict
2. `registry.translate(raw_weights)` → hostname-keyed weights dict
3. If translation yields empty and `all_backends` is known → fall back to equal weights
4. `adapter.set_upstream_weights(translated)`

### `handle_anomaly(payload, registry, adapter)`

```python
backend_name = registry.translate_one(payload["backend_id"])
if payload["status"] == "unhealthy":
    adapter.exclude_backend(backend_name)
else:   # "healthy" or "degraded"
    adapter.include_backend(backend_name)
```

### `handle_policy(payload, adapter, all_backends)`

```python
if not payload.get("safe_mode", False):
    return PolicyOutcome(applied=False, safe_mode=False)
# safe_mode=True: revert to equal weights (but preserve exclusions)
equal_weights = {b: 1 for b in all_backends}
adapter.set_upstream_weights(equal_weights)
```

The policy handler does NOT clear exclusions — backends excluded by anomaly events stay
excluded even under safe_mode. This is intentional: safe_mode means "stop RL-driven weighting"
not "restore all backends to pool".

---

## 7. NginxAdapter — LoadBalancerAdapter Implementation

**File:** `services/shared/lb_adapters/nginx/__init__.py`

### Internal state

The adapter maintains two in-memory data structures:
- `_weights: dict[str, int]` — current weight per backend hostname
- `_excluded: set[str]` — backends currently marked `down`

Both are initialized by `_load_state_from_conf()` at construction time, which parses the
existing `upstream.conf` to extract weights and down-flagged backends. This means the adapter
survives restarts with the last-written state.

### `set_upstream_weights(backend_weights: dict[str, int])`

1. Update `_weights` with the new values (merge, not replace — backends not in the incoming
   dict keep their previous weight)
2. Call `_render_conf()` → NGINX upstream block string
3. `_atomic_write(conf_string)` → write to a tmp file, `os.replace()` into position
4. `_reload_nginx()` → `docker exec <NGINX_CONTAINER> nginx -s reload`

**Idempotency:** If the computed weights dict matches the current `_weights` exactly (and no
exclusions changed), the method is a no-op. No file write, no nginx reload.

### `exclude_backend(backend_id)` / `include_backend(backend_id)`

Add/remove from `_excluded` set, then re-render and reload. If the set doesn't change (e.g.,
excluding an already-excluded backend), the method is a no-op.

### `_render_conf()`

```
upstream backend_pool {
    server smartload-test-backend-1:8080 weight=90 max_fails=3 fail_timeout=10s;
    server smartload-test-backend-2:8080 weight=70 max_fails=3 fail_timeout=10s;
    server smartload-test-backend-3:8080 down;
    ...
}
```

Excluded backends get `down` (no weight) instead of a weight directive. All other backends
get their weight from `_weights`. Backends in `_weights` but not in `_excluded` get rendered
as active servers.

### `current_state()` → `AdapterState`

Returns `AdapterState(upstream_weights=dict(_weights), excluded_backends=list(_excluded))`.
The `/api/v1/lb/state` endpoint calls this. No locks needed since Python's GIL protects
dict reads; the adapter's in-memory state is the source of truth.

### `_reload_nginx()`

```python
container = docker_client.containers.get(nginx_container)
result = container.exec_run("nginx -s reload")
if result.exit_code != 0:
    raise RuntimeError(f"nginx -s reload failed: {result.output}")
```

On Windows (Docker Desktop), `exec_run` takes 15–25 seconds. On Linux, < 1 second. The
caller (run loop) does not time out the reload — it blocks until the exec completes.

---

## 8. BackendRegistry — IP-to-Name Bridge

**File:** `services/lb-sidecar/runloop.py` (same file as handle_* functions)

### Problem

rl-engine sources backend IDs from `$upstream_addr` (the IP:port that NGINX actually used
to forward the request). This is the IP address assigned to the Docker container, not its
hostname. NGINX's upstream block uses hostnames (`smartload-test-backend-1:8080`). The two
are different, and they must be bridged.

### Solution

`BackendRegistry` queries the Docker SDK for all running containers' IP addresses:

```python
def refresh(self) -> None:
    containers = self._docker.containers.list()
    new_map = {}
    for c in containers:
        name = c.name
        networks = c.attrs["NetworkSettings"]["Networks"]
        for net_info in networks.values():
            ip = net_info["IPAddress"]
            ports = c.attrs["NetworkSettings"]["Ports"]
            for port_proto in ports:
                port = port_proto.split("/")[0]
                new_map[f"{ip}:{port}"] = f"{name}:{port}"
    self._map = new_map
```

### `translate(weights: dict[str, int]) → dict[str, int]`

For each key in `weights`:
- If the key is in `_map` (IP-based) → replace with `_map[key]` (hostname-based)
- If the key looks like an IP but isn't in `_map` → trigger `refresh()` once, retry
- If the key does NOT look like an IP (e.g., already a hostname) → pass through unchanged

The pass-through behavior means the walk script can publish envelopes with hostname-based
backend_ids directly and they will arrive unmodified at the adapter.

### Thread-safety

`_map` is replaced atomically with a new dict object (`self._map = new_map`). Callers that
read a stale snapshot during a refresh see the previous mapping — benign stale read.

---

## 9. app.py — Flask Layer and Env Vars

### Env vars

| Var | Default | Purpose |
|---|---|---|
| `LB_SIDECAR_RUNLOOP_ENABLED` | `false` | Enable run loop (safe opt-in) |
| `REDIS_URL` | `redis://redis:6379` | Redis connection |
| `NGINX_CONTAINER` | `smartload-load-balancer-1` | Container name for `docker exec` |
| `NGINX_CONF_PATH` | `/nginx-conf/upstream.conf` | Path to the include file |
| `LB_ADAPTER` | `nginx` | Adapter selection (future: `haproxy`, `envoy`) |
| `POLL_INTERVAL_SECONDS` | `5` | `get_message(timeout=...)` cadence |
| `ALL_BACKENDS` | comma-sep list of 5 backends | BackendRegistry seed + fallback list |
| `PORT` | `8087` | Flask port |

`LB_SIDECAR_RUNLOOP_ENABLED=false` is the default. The service starts, serves `/health`,
but does NOT subscribe to Redis or write any files until the flag is set to `true`.

### `/health` response

When `RUNLOOP_ENABLED=false`:
```json
{"status": "ok", "service": "lb-sidecar", "redis": true}
```

When `RUNLOOP_ENABLED=true`:
```json
{
  "status": "ok",
  "service": "lb-sidecar",
  "redis": true,
  "sidecar_ready": true,
  "last_routing_age_seconds": 3.2,
  "excluded_backends": []
}
```

`sidecar_ready` is set to `true` only after the adapter and BackendRegistry are successfully
initialized (Docker client connected, initial `refresh()` completed, initial upstream.conf
read). If Docker is unreachable, `sidecar_ready` stays `false` and the run loop logs the
error and returns without subscribing to Redis.

### `/api/v1/lb/state`

Returns `adapter.current_state()` as JSON. Returns 503 if the run loop is disabled or not
yet ready.

### `/api/v1/lb/weights` (POST)

Operator-supplied weight override. Accepts `{backend_id: weight, ...}` JSON body. Calls
`adapter.set_upstream_weights(weights)` directly, bypassing the registry translation (the
operator is expected to use NGINX hostnames). Returns 503 if run loop not ready.

### Run loop — Redis pub/sub pattern

```python
redis_client = redis_lib.from_url(REDIS_URL)
pubsub = redis_client.pubsub()
pubsub.subscribe(ROUTING_CHANNEL, ANOMALY_CHANNEL, POLICY_CHANNEL)

while True:
    message = pubsub.get_message(ignore_subscribe_messages=True,
                                 timeout=POLL_INTERVAL_SECONDS)
    if message is None or message.get("type") != "message":
        continue
    # dispatch to handle_routing / handle_anomaly / handle_policy
```

`get_message(timeout=5)` blocks for up to 5 seconds waiting for a message, then returns
`None`. This means the loop fires within 5 seconds of a message arriving. There is no
polling overhead beyond `pubsub.get_message` itself — Redis pub/sub delivers messages as
they arrive.

---

## 10. nginx-conf Volume Bootstrap Problem

### Problem statement

NGINX needs `upstream.conf` to exist inside the `nginx-conf` volume at startup, or it
crashes with:

```
nginx: [emerg] open() "/etc/nginx/conf.d/upstream.conf" failed (No such file or directory)
```

But `lb-sidecar` creates `upstream.conf` — and lb-sidecar depends on `load-balancer`
being up (so it can reach the Docker socket and NGINX container). These form a
chicken-and-egg dependency: NGINX can't start without the file, lb-sidecar can't create
the file until NGINX is running.

### Solution

The file is seeded by the NGINX container itself before NGINX starts, using the
`/docker-entrypoint.d/` hook mechanism built into the nginx base image. The base image
executes all shell scripts in `/docker-entrypoint.d/` before launching `nginx`.

Two additions to the `load-balancer` image:

**`services/load-balancer/nginx/00-seed-upstream.sh`** (new file):
```sh
#!/bin/sh
# Seed the shared nginx-conf volume with a default upstream.conf on first start.
# The lb-sidecar will overwrite this file once it receives routing signals.
if [ ! -f /etc/nginx/conf.d/upstream.conf ]; then
    cp /etc/nginx/upstream.conf.default /etc/nginx/conf.d/upstream.conf
fi
```

**`services/load-balancer/nginx/Dockerfile`** (modified):
```dockerfile
FROM nginx:1.25-alpine

COPY nginx.conf /etc/nginx/nginx.conf
COPY conf.d/upstream.conf /etc/nginx/upstream.conf.default   # baked into image
COPY 00-seed-upstream.sh /docker-entrypoint.d/00-seed-upstream.sh
RUN chmod +x /docker-entrypoint.d/00-seed-upstream.sh

EXPOSE 80
```

The default `upstream.conf` baked into the image has all 5 backends at `weight=1` — equal
round-robin. The volume file is only seeded if it doesn't exist yet (idempotent on restarts).
Once lb-sidecar receives a routing recommendation and writes the volume file, the seed script
becomes a no-op.

---

## 11. docker-compose Changes

```yaml
lb-sidecar:
  build:
    context: ./services
    dockerfile: lb-sidecar/Dockerfile
  ports:
    - "8087:8087"
  environment:
    PORT: "8087"
    SERVICE_NAME: lb-sidecar
    REDIS_URL: ${REDIS_URL:-redis://redis:6379}
    NGINX_CONTAINER: ${NGINX_CONTAINER:-smartload-load-balancer-1}
    NGINX_CONF_PATH: /nginx-conf/upstream.conf
    LB_SIDECAR_RUNLOOP_ENABLED: ${LB_SIDECAR_RUNLOOP_ENABLED:-false}
  volumes:
    - nginx-conf:/nginx-conf               # shared with load-balancer
    - /var/run/docker.sock:/var/run/docker.sock   # Docker SDK for nginx reload
  depends_on:
    - redis
    - load-balancer
  networks:
    - smartload-net

load-balancer:
  # existing config unchanged except:
  volumes:
    - nginx-logs:/nginx-logs
    - nginx-conf:/etc/nginx/conf.d         # new volume mount

volumes:
  nginx-conf:          # new
  nginx-logs:
  timescaledb-data:
```

The Docker socket mount follows the same pattern as the autoscaler (which also uses the
Docker SDK to start/stop containers).

---

## 12. Test Suite Map — lb-sidecar

231 tests pass across all suites. This is independent of the rl-engine test count (124).

### Unit tests (`tests/unit/lb-sidecar/`)

| File | Coverage |
|---|---|
| `test_runloop.py` | `BackendRegistry` (translate, translate_one, refresh), `scores_to_weights`, `handle_routing` (shadow gate, active path, error path), `handle_anomaly` (exclude, include), `handle_policy` (safe_mode, no-op) |
| `test_nginx_adapter.py` | `NginxAdapter` with tmpdir conf + mock docker exec: `set_upstream_weights`, `exclude_backend`, `include_backend`, `current_state`, idempotency, atomic write, reload failure path |

### Conformance tests (`tests/conformance/lb_adapter/`)

Written against a protocol-level mock of `LoadBalancerAdapter` AND the real `NginxAdapter`
(with tmp files + mock exec). Tests:

- Idempotency of all four mutation methods
- `current_state()` reflects each mutation
- All-excluded fallback does not crash (NGINX gets a `down` for every server)
- `weight=0` never written (would be a syntax error; `max(1, ...)` prevents this)

### E2E tests (`tests/e2e/lb-sidecar/`)

Run against a live compose stack. Tests verify the full round-trip:
- `/health` returns `sidecar_ready=true`
- `GET /api/v1/lb/state` returns expected weight structure
- `POST /api/v1/lb/weights` applies overrides and `GET` confirms them
- Redis `mode=active` envelope → weight reflected in `/api/v1/lb/state` within deadline

---

## 13. Smoke Test Execution

The smoke test was the first full end-to-end validation of T2.1 against a live compose
stack. It ran in two phases: shadow gate first, then active actuation.

### Stack start command

```bash
RL_RUNLOOP_ENABLED=true RL_POLICY=ppo LB_SIDECAR_RUNLOOP_ENABLED=true \
docker compose up -d --build --scale test-backend=5
```

`RL_MODE` was NOT set (defaults to `shadow`). This is deliberate: the first phase of the
smoke test verifies the shadow gate before testing active actuation.

### rl-engine WINDOW_SECONDS=30 constraint

The rl-engine only publishes envelopes when `RL_STATE_QUERY` returns at least one row from
the last 30 seconds. Seeding metrics with `--window-minutes 30` generates data from 30
minutes ago — all outside the window. Fix: reseed immediately before subscribing to Redis
with `--window-minutes 1` so the newest seed rows fall within the 30 s window on the next
run loop cycle.

### Issues encountered during startup

The first `docker compose up` attempt failed because the nginx-conf volume was empty (the
`00-seed-upstream.sh` seed script and Dockerfile changes hadn't been made yet). NGINX crashed
immediately. The fix (seed script + Dockerfile) was applied, the image was rebuilt, and
the stack started cleanly.

Test-backend containers were initially unhealthy due to high startup latency (first health
check showed 3844 ms latency against the 30 s start_period). After self-healing (~25 min),
the remaining services came up normally.

On Windows, `localhost` resolves to IPv6 `::1` before IPv4 `127.0.0.1`. Requests to
`http://localhost:8087` timed out. Switching to `http://127.0.0.1:8087` resolved this.

---

## 14. Bugs Found and Fixed During Smoke Test

### Bug 1 — Walk script published non-canonical envelope (critical)

**Symptom:** `lb_sidecar_walk.py` Step 5 published a `mode=active` envelope to
`smartload.routing`, but the lb-sidecar never applied the weights. The poll loop timed out
with a WARN, and the lb-sidecar logs showed no `[lb-sidecar] routing applied` line.

**Root cause:** `parse_envelope()` in `services/shared/contracts.py` validates that the
envelope has both `payload` AND `timestamp` fields at the top level:

```python
if not isinstance(data, dict) or "payload" not in data or "timestamp" not in data:
    _drop(DROP_REASON_NOT_AN_ENVELOPE)
    return None
```

The walk script's envelope was:
```python
{
    "source": "lb-sidecar-walk",
    "channel": "smartload.routing",
    "payload": { "mode": "active", ... }
}
```

It was missing `event_id`, `version`, and `timestamp` at the top level. `parse_envelope`
silently dropped it — the lb-sidecar's run loop called `parse_envelope`, got `None`, and
`continue`d to the next iteration.

**Fix:** Added canonical envelope fields to the walk script:
```python
import uuid
from datetime import datetime, timezone

envelope = json.dumps({
    "event_id": str(uuid.uuid4()),
    "source": "lb-sidecar-walk",
    "version": 1,
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "payload": { ... }
})
```

**Scope — rl-engine publish path was never affected.** The bug was isolated to the walk
script. The real rl-engine publisher (`services/rl-engine/app.py:176`) was verified clean
post-smoke: it calls `publish_envelope()` from `shared/contracts.py`, which calls
`make_envelope()`, which fills all canonical fields automatically:

```python
# app.py:176 — the exact publish call
publish_envelope(
    redis_client,
    channel=ROUTING_CHANNEL,                                     # "smartload.routing"
    source=SERVICE_NAME,                                         # "rl-engine"
    payload=action_to_event_payload(action, mode, eng_policy.policy_version),
)

# contracts.py — what publish_envelope serialises to Redis
{
    "event_id":  str(uuid.uuid4()),                              # auto-generated
    "source":    "rl-engine",
    "version":   1,                                              # ENVELOPE_VERSION constant
    "timestamp": datetime.now(timezone.utc).isoformat(),         # _now_iso() UTC
    "payload": {
        "mode":            "shadow",           # or "active"
        "server_rankings": [{"backend_id": ..., "score": ...}, ...],
        "policy_version":  19,
    }
}
```

`action_to_event_payload` (`runloop.py:250`) builds only the `payload` dict; the four
outer fields are added by `make_envelope`. Every other service that calls `publish_envelope`
gets the same guarantee. The walk script was the only caller that bypassed this helper.

**Lesson:** Any manual publisher of Redis envelopes must include all four top-level envelope
fields. `parse_envelope()` does not log the reason for drops at the caller site — the only
way to detect a DROP_REASON_NOT_AN_ENVELOPE is to add `on_drop` callback instrumentation
or check the lb-sidecar logs for the absence of a `routing applied` line. Use
`publish_envelope()` or `make_envelope()` from `shared/contracts.py`; never hand-craft
the outer envelope JSON.

### Bug 2 — httpx timeout too short for Windows docker exec (minor)

**Symptom:** `POST /api/v1/lb/weights` timed out after 10 s. The docker exec reload takes
15–25 s on Docker Desktop for Windows.

**Fix:** `timeout=10.0` → `timeout=30.0` in the httpx client.

### Bug 3 — Step 5 poll deadline too short (minor)

**Symptom:** Walk script Step 5 polled for weight update for 10 s, then emitted a WARN.
After the timeout, the weight DID eventually update (lb-sidecar logs showed `routing applied`
after the poll expired).

**Fix:** Poll deadline extended from 10 s to 35 s. The lb-sidecar's run loop must: receive
the message on the next 5 s get_message cycle, call set_upstream_weights (which includes the
docker exec reload), then the poll reads the updated in-memory state. Total latency on
Windows: up to 5 s (message delivery) + 25 s (reload) = 30 s.

### Bug 4 — Windows CP1252 UnicodeEncodeError (minor)

**Symptom:** Walk script crashed on the final success print with:
```
UnicodeEncodeError: 'charmap' codec can't encode character '→' in position ...
```

Windows console uses CP1252 encoding by default. U+2192 (`→`) and U+2713 (`✓`) are not
in CP1252.

**Fix:** Replaced both occurrences of `→` with `->` and `✓` with `OK` in the walk script.

### Bug 5 — nginx-conf volume empty on first start (infrastructure)

Described fully in §10 above. NGINX crashed because the `nginx-conf` volume was empty and
the `include` directive couldn't find `upstream.conf`. Fixed by adding the `00-seed-upstream.sh`
bootstrap script to the NGINX image.

### Bug 6 — .gitignore blocked *.log files in docs/runs/

**Symptom:** `git add docs/runs/.../lb_sidecar.log` failed:
```
The following paths are ignored by one of your .gitignore files:
docs/runs/t2_1_smoke_20260523_213133/lb_sidecar.log
```

The `.gitignore` had a Django-era `*.log` rule at line 62. The negation pattern
`!docs/runs/**/*.log` was placed before this line (line 40) and was therefore overridden
by the later `*.log` rule (gitignore: last matching rule wins).

**Fix:** Moved the negation to immediately after the `*.log` rule:
```
# Django stuff:
*.log
# Smoke-run artifacts under docs/runs/ are deliberately committed
!docs/runs/**/*.log
```

---

## 15. Smoke Test Results and Artifacts

### Walk script output (final passing run)

All 6 steps passed:

```
Step 1: Check /health
  OK  sidecar_ready=true, excluded_backends=[]

Step 2: Read /api/v1/lb/state
  OK  5 upstream backends, 0 excluded
       smartload-test-backend-1:8080: weight=1
       ...

Step 3: POST /api/v1/lb/weights (5 backends)
  OK  applied_weights: {backend-1: 80, backend-2: 60, backend-3: 40, backend-4: 40, backend-5: 40}

Step 4: Verify state reflects custom weights
  OK  weights match custom overrides

Step 5: Publish active RoutingRecommendation via Redis -> watch weights update
  -> envelope published to smartload.routing
  OK  weight updated: backend-1=99 (from score=0.99)

Step 6: Restore equal weights
  OK  equal weights restored

OK lb-sidecar scenario complete
```

### What Step 5 proves

The walk publishes a `mode=active` envelope with scores `{backend-1: 0.99, backend-2: 0.70, ...}`.
The lb-sidecar receives it via pub/sub, `parse_envelope` succeeds (canonical fields present),
`handle_routing` is called with `mode="active"`, `scores_to_weights` computes `{backend-1: 99, ...}`,
`registry.translate` passes backend hostnames through unchanged, `adapter.set_upstream_weights`
writes the conf and signals reload. The walk then polls `/api/v1/lb/state` and reads
`backend-1 weight=99`. The lb-sidecar log confirms: `[lb-sidecar] routing applied (5 backends)`.

### Shadow gate evidence

Before running the walk, the stack was running in shadow mode (`RL_MODE` not set). The
rl-engine published envelopes with `mode=shadow`. The lb-sidecar's `handle_routing` returned
early without calling the adapter. `upstream.conf` remained at all `weight=1`. This was
verified by:
- Subscribing to `smartload.routing` and capturing one envelope (`shadow_envelope.json`)
- Reading `/nginx-conf/upstream.conf` from inside the lb-sidecar container (`upstream_conf_shadow.txt`)

### Traffic test

```
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/  × 20
```

Result: 20/20 HTTP 200. NGINX served traffic throughout the test with no interruption, even
during the `nginx -s reload` in Step 5.

### Error scan

```
docker compose logs --tail=100 lb-sidecar rl-engine load-balancer | grep -iE "error|traceback|exception"
```

Result: zero matches. No errors, tracebacks, or exceptions in any of the three critical
service logs.

### Committed artifacts

All artifacts are in `docs/runs/t2_1_smoke_20260523_213133/`:

| File | What it proves |
|---|---|
| `shadow_envelope.json` | rl-engine published `mode=shadow`; Redis delivered it |
| `upstream_conf_shadow.txt` | lb-sidecar did NOT rewrite `upstream.conf` in shadow mode |
| `upstream_conf_active.txt` | Post-walk state (equal weights after Step 6 restore); Step 5 confirmed backend-1=99 |
| `walk_output.txt` | All 6 steps passed |
| `lb_sidecar.log` | lb-sidecar container logs |
| `lb_sidecar_final.log` | Final lb-sidecar logs showing `routing applied (5 backends)` |
| `rl_engine.log` | rl-engine logs showing `rl_mode=shadow` run loop startup |
| `compose_ps.txt` | All 21 services running |
| `README.md` | Artifact manifest with what each file proves |

---

## 16. Updated Phase Status

The milestone table from Part 1 (§3) is updated here:

| Milestone | Description | Status | Key commit |
|---|---|---|---|
| T2.1 | LB sidecar + NginxAdapter | **Done** | `398ed99` |
| T2.1 smoke test | End-to-end validation | **PASSED** | `d37e209` |
| N2.3 | SmartLoadEnv | Done | `787eba8` |
| N2.4 | Training pipeline + policy.zip | Done | `15991db` |
| N2.5 | PPOPolicy serving plugin | Done | `787eba8` |

**T2.1 unblocks:** `RL_MODE=active` is now usable end-to-end. To activate PPO-driven
routing in the live stack, set `RL_MODE=active RL_RUNLOOP_ENABLED=true RL_POLICY=ppo
LB_SIDECAR_RUNLOOP_ENABLED=true` and restart both services. The policy.yaml already has
`operating_mode=hybrid safe_mode=false` — no policy change is needed.

**Still pending Phase 2 items** (unchanged from Part 1):
- Operator UI — Live Engines view (#121)
- Python SDK full implementation (#127)
- Webhook dispatcher (#130)
- Isolation Forest anomaly model (#101)
- ARIMA forecasting model (#102)
- Strict lint mode (#139)

---

## 17. Key Invariants — lb-sidecar Additions

These extend the invariants in Part 1 §21.

8. **Every Redis envelope must include `event_id`, `version`, `timestamp`, and `payload` at
   the top level.** `parse_envelope()` silently drops (`DROP_REASON_NOT_AN_ENVELOPE`) any
   message missing `payload` or `timestamp`. It does not log the reason at the caller site.
   Use `make_envelope()` or `publish_envelope()` from `services/shared/contracts.py`
   instead of hand-crafting JSON.

9. **`upstream.conf` must always contain at least one non-`down` server line.** If every
   backend is excluded, NGINX will fail to start or return 502 on all requests. The
   adapter does not enforce this — it is the caller's responsibility to never exclude all
   backends simultaneously. `handle_routing`'s all-backends fallback (equal weights when
   translation yields empty) partially mitigates this, but anomaly-driven exclusion of all
   backends is still possible.

10. **The `nginx-conf` volume must be created before the load-balancer container starts.**
    Docker Compose creates volumes before containers, so this is automatic in a fresh
    `docker compose up`. But `docker run` invocations that skip Compose may not create the
    volume. The seed script handles the empty-volume case, but it cannot create the volume
    itself.

11. **`docker exec nginx -s reload` takes 15–25 s on Docker Desktop for Windows.** Any
    HTTP client hitting `/api/v1/lb/weights` or the walk script must use a timeout of at
    least 30 s. This is a Windows-specific constraint — on Linux, the same exec takes < 1 s.

12. **The gitignore negation `!docs/runs/**/*.log` must appear AFTER `*.log` in `.gitignore`.**
    gitignore processes rules top-to-bottom; the last matching rule wins. If the negation
    is placed before the `*.log` rule, the `*.log` rule overrides it and log files in
    `docs/runs/` cannot be committed.

---

*Document generated: 2026-05-24. Covers commits `398ed99` through `dca9403`. Cross-checked
against: `services/lb-sidecar/app.py`, `runloop.py`, `Dockerfile`; `services/shared/lb_adapters/nginx/__init__.py`;
`services/shared/contracts.py`; `services/load-balancer/nginx/Dockerfile`, `00-seed-upstream.sh`;
`docker-compose.yml`; `examples/scenarios/lb-sidecar/lb_sidecar_walk.py`;
`docs/runs/t2_1_smoke_20260523_213133/`; `docs/SOURCE_OF_TRUTH.html` v1.0.5.*
