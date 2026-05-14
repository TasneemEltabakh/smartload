# SmartLoad — End-to-end pipeline walkthrough

> Captured: 2026-05-14
> Stack version: slice #1 (vertical) — policy-management end-to-end
> Working tree: local `main` at 28 commits ahead of `origin/main` (15 feature branches pushed)
> Audience: anyone who wants to verify, demo, or reproduce the full pipeline locally

This document is the canonical "run it yourself" reference for the SmartLoad stack at
its current state. It records every command, every expected output, every URL, every
verification — in the same order they were executed during the live walkthrough.

---

## 1. TL;DR

Bring the stack up (~20 containers), open seven browser tabs, exercise the operator UI,
hit `/api/docs`, drive traffic through NGINX, watch Grafana, then run the Python SDK
quickstart + the policy-management scenario script + the e2e test suite. Everything
green; 10/10 e2e tests pass in 6.26s.

| Surface | URL | What it proves |
|---|---|---|
| Operator UI Home | `http://localhost:8090/` | Service-health grid; 7 services up; redis + timescaledb both `true` |
| Operator UI Policy | `http://localhost:8090/policy` | Read → JSON edit → diff preview → commit → audit row appears |
| Swagger UI | `http://localhost:8090/api/docs/` | OpenAPI 3.1 spec rendered; every shipped `/api/v1/*` route present |
| NGINX LB | `http://localhost:8080/` | Routes traffic to 5 `test-backend` replicas |
| Locust | `http://localhost:8089/` | Synthetic load generator; 28 RPS sustained, 0 failures |
| Grafana | `http://localhost:3000/d/smartload-overview` | Live request rate / latency / error rate / active backends |
| Prometheus | `http://localhost:9090/targets` | OTel Collector scraped cleanly |

Plus:

```
python clients/python/examples/quickstart.py                         → prints policy
python examples/scenarios/policy-management/policy_walk.py           → exits 0; envelope received
pytest tests/e2e/policy-management/test_policy_walk.py -v            → 10/10 PASSED in 6.26s
```

---

## 2. Stack bring-up

### Preconditions

- Docker Desktop installed and running
- Working tree clean (or at least the operator-ui sources present at
  `services/operator-ui/{bff,web}`)
- Repo cloned at `G:/smartload` (paths in this doc assume that root)

### Commands

```bash
# (1) Confirm Docker is reachable
docker --version
docker compose version
docker ps --format "table {{.Names}}\t{{.Status}}"

# (2) Bring up the stack with the canonical 5 test-backend replicas
docker compose up -d --scale test-backend=5

# (3) Confirm all services healthy
docker compose ps --format "table {{.Name}}\t{{.Status}}"
```

### Observed output (Stack ready)

```
NAME                            STATUS
smartload-anomaly-detector-1    Up
smartload-autoscaler-1          Up
smartload-forecasting-1         Up
smartload-grafana-1             Up
smartload-lb-otel-shipper-1     Up
smartload-load-balancer-1       Up
smartload-operator-ui-1         Up
smartload-otel-collector-1      Up
smartload-policy-manager-1      Up
smartload-prometheus-1          Up
smartload-redis-1               Up (healthy)
smartload-rl-engine-1           Up
smartload-telemetry-1           Up
smartload-test-backend-1        Up (healthy)
smartload-test-backend-2        Up (healthy)
smartload-test-backend-3        Up (healthy)
smartload-test-backend-4        Up (healthy)
smartload-test-backend-5        Up (healthy)
smartload-timescaledb-1         Up (healthy)
smartload-traffic-simulator-1   Up
```

**Expected**: 20 containers running. `redis`, `timescaledb`, and every `test-backend`
replica should show `(healthy)`.

### One-shot smoke check

```bash
curl -s -o /dev/null -w "policy-manager  %{http_code}\n" http://localhost:8086/health
curl -s -o /dev/null -w "operator-ui     %{http_code}\n" http://localhost:8090/api/ui/health
curl -s -o /dev/null -w "load-balancer   %{http_code}\n" http://localhost:8080/
curl -s -o /dev/null -w "telemetry       %{http_code}\n" http://localhost:8081/health
curl -s -o /dev/null -w "grafana         %{http_code}\n" http://localhost:3000/api/health
curl -s -o /dev/null -w "swagger ui      %{http_code}\n" http://localhost:8090/api/docs/
curl -s -o /dev/null -w "locust          %{http_code}\n" http://localhost:8089/
```

**Expected output** — all 200:

```
policy-manager  200
operator-ui     200
load-balancer   200
telemetry       200
grafana         200
swagger ui      200
locust          200
```

---

## 3. Tab 1 — Operator UI Home

**URL**: `http://localhost:8090/`

### What you should see

- Sidebar: `SmartLoad / Operator UI` heading; nav links `Home`, `Policy`, `API docs`
- Main panel header: **Service health** with subtext "Polled every 10s · …"
- A 4-column grid of pill cards, one per SmartLoad service, each showing:
  - service name
  - status (`ok` or `healthy`)
  - HTTP code
  - `redis=true · timescaledb=true` (where applicable)

### Expected pills (current build)

| Pill | Status | Notes |
|---|---|---|
| anomaly-detector | ok · HTTP 200 · redis=true · timescaledb=true | green left border |
| autoscaler | ok · HTTP 200 · redis=true · timescaledb=true | green |
| forecasting | ok · HTTP 200 · redis=true · timescaledb=true | green |
| load-balancer | healthy · HTTP 200 | **orange** — NGINX returns `"healthy"` not `"ok"` (known visual quirk) |
| policy-manager | ok · HTTP 200 · redis=true · timescaledb=true | green |
| rl-engine | ok · HTTP 200 · redis=true · timescaledb=true | green |
| telemetry | ok · HTTP 200 · redis=true · timescaledb=true | green |

The orange `load-balancer` pill is **not a problem** — it just means the upstream
returned a status string the UI's exact-match classifier didn't recognize. NGINX is
fully functional; only the visual tag is off.

---

## 4. Tab 2 — Operator UI Policy page

**URL**: `http://localhost:8090/policy`

### What you should see — four cards top-to-bottom

1. **Current policy** — formatted JSON of all 12 canonical fields. Sub-header shows
   `policy_version = <N> · operating_mode = <hybrid|classical-only|rl-only> · safe_mode = <bool>`.
2. **Edit (JSON)** — textarea pre-filled with the same JSON. Two buttons: `Commit`,
   `Reset`. Live JSON validation — invalid JSON disables `Commit`.
3. **Diff preview** — side-by-side viewer (`react-diff-viewer-continued`) comparing
   current vs draft. Lines collapsed where identical; expand-line links available.
4. **Recent audit (N)** — table with columns `Time · Version · Field · Old · New · Actor`.

### Workflow demonstration — flip `safe_mode`

The walkthrough flipped `safe_mode` from `false` to `true` via the UI:

1. Page loads — current policy reads `safe_mode: false`, audit shows 4 prior rows
   (from integration test runs).
2. Modify the textarea: replace `"safe_mode": false,` with `"safe_mode": true,`.
3. Diff preview updates: line 12 highlighted red on the left
   (`"safe_mode": false,`), green on the right (`"safe_mode": true,`).
4. Click `Commit`. Browser POSTs `{ safe_mode: true }` (no `policy_version` — server
   manages it) with header `X-Actor: operator-ui`.
5. Toast confirms: `updated (v1; changed safe_mode)`.
6. Page re-fetches both panels. New audit row at the top:
   `2026-05-14T11:03:12 | 1 | safe_mode | false | true | operator-ui`.
7. Current-policy card now reads `policy_version = 1 · safe_mode = true`.

### What's happening behind the UI

```
Browser POST → BFF /api/ui/policy → policy-manager POST /api/v1/policy
                                          │
                                          ├── validate (services/policy-manager/validation.py)
                                          ├── atomic write → config/policy.yaml
                                          ├── insert row → policy_changes hypertable
                                          └── publish envelope → smartload.policy
```

Subscribers (autoscaler, anomaly-detector, etc.) receive the new policy within one
poll cycle — that's how live reload is achieved without a service restart.

---

## 5. Tab 3 — Swagger UI

**URL**: `http://localhost:8090/api/docs/`

### What you should see

- Swagger UI header: **SmartLoad API 1.0.0 — OAS 3.1**
- Server selector pre-populated with `http://localhost:8086 — policy-manager (local dev)`
- Three tag sections:
  - **policy** — `GET /api/v1/policy`, `POST /api/v1/policy`, `GET /api/v1/audit/policy`
  - **telemetry** — `GET /api/v1/metrics`, `GET /api/v1/stats`
  - **audit** — `GET /api/v1/audit/policy` (cross-tag entry)
- A `default` section with `GET /health`
- A **Schemas** section listing 9 named types: `HealthOk`, `HealthDegraded`, `Policy`,
  `PolicyPatch`, `PolicyUpdateResponse`, `PolicyAuditRow`, `MetricRow`,
  `TelemetryStats`, `Error`

### Try-it-out

Click any endpoint, click `Try it out`, click `Execute`. Swagger UI proxies the
request through the BFF (server URL set to localhost:8086). For `GET /api/v1/policy`
you should get a 200 response with the current policy JSON.

### Behind the scenes

The spec file is `docs/openapi/smartload-v1.yaml`, mounted into the operator-ui
container as a read-only volume at `/app/openapi/smartload-v1.yaml`. The BFF's
`flask-swagger-ui` blueprint serves the rendered UI; the raw spec is at
`http://localhost:8090/api/openapi.yaml`.

The CI lint `scripts/lint-openapi.py` enforces that every `/api/v1/*` route in
`services/` source appears in this spec.

---

## 6. Tab 4 — NGINX load balancer (data plane)

**URL**: `http://localhost:8080/`

### Smoke test

```bash
curl -s http://localhost:8080/
```

**Expected output** (one of):
```
Hello from 001ec07c50b6
```
or any other test-backend container ID. The hostname rotates because NGINX is using
round-robin across the 5 replicas.

### Generate burst traffic

```bash
for i in 1 2 3 4 5 6 7 8 9 10; do
  curl -s -o /dev/null -w "%{http_code} " http://localhost:8080/
done
echo
```

**Expected**: ten `200`s in a row.

### Where it ends up

Every request:
1. Logs to NGINX access log (volume `nginx-logs`)
2. Read by `lb-otel-shipper` sidecar
3. Shipped as OTLP/HTTP-JSON to `otel-collector:4318`
4. Forwarded to `telemetry` service
5. Persisted in `metrics` hypertable (TimescaleDB)
6. Aggregated minutely by the `metrics_1min` continuous aggregate
7. Rendered by Grafana panels

---

## 7. Tab 5 — Locust traffic simulator

**URL**: `http://localhost:8089/`

### Start a load run via REST

```bash
curl -s -X POST http://localhost:8089/swarm \
     -d "user_count=20&spawn_rate=5&host=http://load-balancer"
```

**Expected response**:
```json
{ "host": "http://load-balancer", "message": "Swarming started", ... }
```

### Watch stats

```bash
sleep 4
curl -s http://localhost:8089/stats/requests | jq
```

**Observed** during the live walkthrough:
- `total_rps`: 28.45
- `req=115 · fail=0 · avg=3ms`

### Stop the swarm

```bash
curl -s http://localhost:8089/stop
```

---

## 8. Tab 6 — Grafana SmartLoad Overview

**URL**: `http://localhost:3000/dashboards` → **SmartLoad** folder → **SmartLoad Overview**

Direct: `http://localhost:3000/d/smartload-overview/smartload-overview?from=now-15m&to=now&refresh=5s`

### Panels

| Panel | Source | What it shows |
|---|---|---|
| Request rate (req/s) | TimescaleDB `metrics` (metric_name = `request_count`) | Spikes to ~37 req/s during the Locust run |
| Request latency — p50 / p95 / max | TimescaleDB `metrics` (metric_name = `request_latency_ms`) | p50 ~1ms, p95 ~3ms, max ~9ms |
| Error rate (%) | TimescaleDB `metrics` (metric_name = `error_rate`) | 0% under healthy load |
| Total requests (in window) | Sum over the 15-minute window | 5.66K during the walkthrough |
| Active backend instances | Distinct `instance` count in the latest minute bucket | 5 |
| Telemetry rows ingested (by metric) | Stacked bar — request_count / error_rate / request_latency_ms | Telemetry-pipeline health |

### Provisioning

- Datasource: `infrastructure/grafana/provisioning/datasources/timescaledb.yaml`
- Dashboards: `infrastructure/grafana/dashboards/*.json`
- Default admin login: `admin` / `admin` (configured via `GRAFANA_PASSWORD` env)

---

## 9. Tab 7 — Prometheus

**URL**: `http://localhost:9090/targets`

### Expected target status

| Job | State | Notes |
|---|---|---|
| `otel-collector` | **UP** | scrape target `http://otel-collector:8889/metrics` |
| `anomaly-detector`, `autoscaler`, `forecasting`, `policy-manager`, `rl-engine`, `telemetry` | **DOWN** | scrape target `/health` returns JSON, not Prometheus exposition format |

The 6 DOWN targets are a **known gap** — issue **#138** (engine-wrapper integration)
adds per-service own-metrics (`<svc>_inferences_total` and friends) that Prometheus
will scrape on a dedicated `/metrics` endpoint. For now, all observable data flows
via the OTel Collector pipeline above (which is `UP`).

---

## 10. Programmatic verification

### 10a. Install the SDK locally (editable)

```bash
pip install -e clients/python
```

**Expected**:
```
Successfully installed smartload-client-0.1.0
```

### 10b. SDK quickstart

```bash
python clients/python/examples/quickstart.py
```

**Expected output** (values reflect current `config/policy.yaml`):
```
operating_mode = hybrid
safe_mode      = True
min_backends   = 1
max_backends   = 5
policy_version = 1
```

The script connects via `SmartLoadClient(base_url="http://localhost:8086")` and calls
`get_policy()`. No Redis activity.

### 10c. Policy-management scenario

```bash
python examples/scenarios/policy-management/policy_walk.py
```

**Expected output**:
```
== policy-management slice walkthrough ==
policy-manager: http://localhost:8086
redis:          redis://localhost:6379

  ok: baseline read (safe_mode=True, policy_version=1)
  -> toggling safe_mode True -> False
  ok: envelope received (policy_version=2, changed_fields=['safe_mode'])
  audit (latest 5 rows):
    - <ts> field=safe_mode new=True actor=policy_walk-restore
    - <ts> field=safe_mode new=False actor=policy_walk
    - <ts> field=safe_mode new=True actor=operator-ui
    - <ts> field=max_backends new=6 actor=anonymous
    - <ts> field=max_backends new=7 actor=pytest-suite

PASS
```

**What the script does**:
1. `client.get_policy()` — snapshot baseline
2. `client.subscribe_policy(callback)` — background daemon thread on `smartload.policy`
3. Drain 300ms of pre-attach backlog
4. `client.set_policy({"safe_mode": <flipped>}, actor="policy_walk")` — POST
5. Wait ≤5s for the matching envelope on the channel
6. `client.set_policy({"safe_mode": <baseline>}, actor="policy_walk-restore")` — restore
7. `client.audit_policy(limit=5)` — print last 5 audit rows
8. Close the subscription

**Exit code 0** on success; **1** on any timeout / mismatch.

### 10d. Full e2e test suite

```bash
pytest tests/e2e/policy-management/test_policy_walk.py -v
```

**Expected output**:
```
collected 10 items

tests/e2e/policy-management/test_policy_walk.py::TestPolicyRead::test_get_returns_canonical_fields PASSED [ 10%]
tests/e2e/policy-management/test_policy_walk.py::TestPolicyRead::test_get_is_idempotent PASSED [ 20%]
tests/e2e/policy-management/test_policy_walk.py::TestPolicyWrite::test_update_returns_changed_fields PASSED [ 30%]
tests/e2e/policy-management/test_policy_walk.py::TestPolicyWrite::test_idempotent_repeat_is_noop PASSED [ 40%]
tests/e2e/policy-management/test_policy_walk.py::TestPolicyWrite::test_invalid_raises_validation_error PASSED [ 50%]
tests/e2e/policy-management/test_policy_walk.py::TestPolicyWrite::test_unknown_operating_mode_raises_validation_error PASSED [ 60%]
tests/e2e/policy-management/test_policy_walk.py::TestPolicySubscribe::test_envelope_arrives_within_5s PASSED [ 70%]
tests/e2e/policy-management/test_policy_walk.py::TestPolicySubscribe::test_callback_exception_does_not_kill_thread PASSED [ 80%]
tests/e2e/policy-management/test_policy_walk.py::TestPolicyAudit::test_audit_returns_recent_change PASSED [ 90%]
tests/e2e/policy-management/test_policy_walk.py::TestPolicyAudit::test_audit_limit_caps_results PASSED [100%]

============================== 10 passed in 6.26s ==============================
```

**What the 10 tests cover**:

| Class | Test | Asserts |
|---|---|---|
| `TestPolicyRead` | `test_get_returns_canonical_fields` | every Policy field present |
| `TestPolicyRead` | `test_get_is_idempotent` | two GETs return equal payloads |
| `TestPolicyWrite` | `test_update_returns_changed_fields` | server reports `changed_fields=["max_backends"]` |
| `TestPolicyWrite` | `test_idempotent_repeat_is_noop` | second identical POST returns `status: "no-op"` |
| `TestPolicyWrite` | `test_invalid_raises_validation_error` | `min > max` → `ValidationError` with `.field` |
| `TestPolicyWrite` | `test_unknown_operating_mode_raises_validation_error` | bad enum → `ValidationError(field="operating_mode")` |
| `TestPolicySubscribe` | `test_envelope_arrives_within_5s` | published change reaches subscriber inside SLO |
| `TestPolicySubscribe` | `test_callback_exception_does_not_kill_thread` | a raising callback doesn't crash the daemon |
| `TestPolicyAudit` | `test_audit_returns_recent_change` | audit row matches the field + actor we wrote |
| `TestPolicyAudit` | `test_audit_limit_caps_results` | `?limit=1` returns ≤1 row |

All ten use the SmartLoad SDK exclusively — proving the SDK is the customer-facing
surface, not just a wrapper.

### 10e. SDK unit tests (no live stack required)

```bash
pytest tests/unit/test_smartload_client.py -v
```

**Expected**: 18 passed in ~0.2s. Covers `_raise_for_status` status-code → typed
exception mapping, plus `parse_envelope` round-trip + bad-input handling + per-channel
TTL behavior.

### 10f. CI structural lints

```bash
python scripts/lint-structure.py
python scripts/lint-openapi.py
python scripts/lint-redis-channels.py
```

**Expected**:
```
3 violations
permissive mode; flip to --strict in CI once warnings are resolved
OK: every /api/v1 route in services/ is documented in OpenAPI
OK: every Redis channel in services/ is documented
```

The 3 lint warnings are intentional stub plugins (`isolation_forest`, `arima`, `ppo`)
that have no `test_*.py` yet — they get tests when their real models land (issues
#101, #102, #27).

---

## 11. Pipeline — causal diagram

The full chain of components that participate in a policy-change operation:

```
        Browser                       SDK / curl                      pytest
           │                              │                              │
           ▼                              ▼                              ▼
   operator-ui:8090   ─────►     policy-manager:8086     ◄────────  e2e test
   (BFF + React SPA)             (REST + validation +
           │                       audit + publish)
           │
           │ /api/ui/policy*               │
           ▼                                │
        policy-manager:8086                 │
                                            ▼
                              ┌─────────────────────────────┐
                              │  POST /api/v1/policy        │
                              │  1. validate input          │
                              │  2. atomic YAML write       │
                              │  3. policy_changes insert   │
                              │  4. publish smartload.policy│
                              └─────────────────────────────┘
                                            │
            ┌───────────────────┬───────────┴───────────┬───────────────┐
            ▼                   ▼                       ▼               ▼
       config/                 policy_changes        smartload.policy   ─►  RL
       policy.yaml            (TimescaleDB)         (Redis pub/sub)         AD
       (atomic rewrite)                                                    AS
                                                                          forecasting
```

And the request-path / telemetry side:

```
   Client ──► NGINX :8080 ──► test-backend (5 replicas)
                 │
                 ▼ access.log (JSON)  [shared volume nginx-logs]
              lb-otel-shipper (sidecar)
                 │
                 ▼ OTLP/HTTP-JSON
              otel-collector :4318
                 │
                 ├──► telemetry :8081 ──► TimescaleDB.metrics
                 │
                 └──► Prometheus scrape (8889/metrics)
                            │
                            ▼
                       Grafana :3000 ──► live dashboard panels
```

---

## 12. Bonus fixes shipped during this walkthrough

Two real bugs surfaced while clicking through the pipeline; both fixed in-place.

### 12a. BFF SPA fallback bug

**Symptom**: `GET /policy` (direct URL access / browser reload) returned 404 instead
of serving the React SPA shell.

**Root cause**: Flask auto-registers a static endpoint when `static_folder` is set;
that endpoint short-circuits with 404 for non-file paths before the manual
`@app.route("/<path:path>")` catch-all gets a chance.

**Fix** (`services/operator-ui/bff/app.py`): replace the catch-all route with an
`@app.errorhandler(404)`. It runs after both the static handler and all explicit
routes, so real `/api/*` 404s stay as 404s but unknown non-API paths now fall through
to `index.html`.

**Verification**:
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8090/policy
# expected: 200
```

### 12b. Stale `policy-manager` image

**Symptom**: `GET /api/ui/audit/policy` (proxied to `policy-manager:8086/api/v1/audit/policy`)
returned 404 even though the route is defined in source.

**Root cause**: the running container was built before the `GET /api/v1/audit/policy`
endpoint was committed; Docker reused the cached image.

**Fix**:
```bash
docker compose up -d --build policy-manager
```

After rebuild, the audit endpoint serves rows from the `policy_changes` hypertable.

**Prevention**: every commit on `feat/policy-audit-endpoint` should be followed by
`docker compose up -d --build policy-manager` in dev. CI builds always start from a
clean image, so this doesn't affect CI.

---

## 13. What was NOT demonstrated (known gaps)

The slice #1 scope is intentionally narrow. The following are documented in the SOT
but not yet runnable / verifiable end-to-end:

| Surface | Tracking | Why it's not shown today |
|---|---|---|
| AI engine run loops (anomaly / forecast / RL) | #138 | Services still ship a Phase-0 `/health` stub; engine plugins exist but aren't wired into `app.py` yet |
| Webhooks delivery | #130 | `webhook-dispatcher` service is scaffolded; no implementation yet |
| Multi-tenancy (`tenant_id`) | #129 | Pure-default-tenant today; schema migration + scoping pending |
| API keys + RBAC | #132 | Endpoints currently accept unauthenticated requests |
| Operator UI Live Engines page | #121 | Slice #1 ships Home + Policy only |
| Operator UI manual actions (scale / isolate) | #123 | Endpoints `POST /api/v1/scale` and `POST /api/v1/isolate` not yet implemented |
| Helm chart templates | #133 | Chart.yaml + values.yaml only; no `templates/` content |
| LB adapter — Envoy / HAProxy / ALB concrete impls | #136 | Interface + stubs only; only `nginx` adapter is wired |
| Trained models (Isolation Forest, ARIMA, PPO) | #101, #102, #27 | Baseline plugins ship; trained models are Nada's track |

---

## 14. Reproducing this walkthrough

### Bare-metal prerequisites

- Docker Desktop ≥ 28 with the Linux engine
- Python 3.10 or newer
- ~6 GB free RAM
- Free ports: 3000, 4317, 4318, 5432, 6379, 8080, 8081–8086, 8089, 8090, 8889, 9090

### Step-by-step

```bash
git clone <repo-url> && cd smartload
cp config/.env.example .env                          # fill values if needed

# Bring the stack up
docker compose up -d --scale test-backend=5

# Wait until everything is healthy (60s should be plenty)
sleep 60
docker compose ps

# Smoke-check the seven surfaces (expect 7x 200)
for tuple in "policy-manager:8086:/health" \
             "operator-ui:8090:/api/ui/health" \
             "load-balancer:8080:/" \
             "telemetry:8081:/health" \
             "grafana:3000:/api/health" \
             "operator-ui-docs:8090:/api/docs/" \
             "locust:8089:/"; do
  name=${tuple%%:*}
  rest=${tuple#*:}
  port=${rest%%:*}
  path=${rest#*:}
  printf "%-20s %s\n" "$name" "$(curl -s -o /dev/null -w "%{http_code}" http://localhost:$port$path)"
done

# Drive some traffic (optional)
curl -s -X POST http://localhost:8089/swarm \
     -d "user_count=20&spawn_rate=5&host=http://load-balancer"

# Install + run the SDK quickstart
pip install -e clients/python
python clients/python/examples/quickstart.py

# Run the scenario
python examples/scenarios/policy-management/policy_walk.py

# Run the e2e suite
pytest tests/e2e/policy-management/test_policy_walk.py -v
```

### Open in the browser (one at a time)

1. `http://localhost:8090/` — Operator UI Home
2. `http://localhost:8090/policy` — Policy editor
3. `http://localhost:8090/api/docs/` — Swagger UI
4. `http://localhost:8080/` — NGINX (responds with `Hello from <backend>`)
5. `http://localhost:8089/` — Locust
6. `http://localhost:3000/d/smartload-overview` — Grafana Overview
7. `http://localhost:9090/targets` — Prometheus scrape targets

---

## 15. Teardown

```bash
# Stop and remove containers but keep volumes (TimescaleDB data + nginx-logs)
docker compose down

# Stop and wipe volumes too (forces a clean DB on next up)
docker compose down -v

# Free disk: prune dangling images / build cache
docker system prune -f
```

---

## 16. Where to go for canonical references

| Question | File |
|---|---|
| Full architecture, every section | `docs/SOURCE_OF_TRUTH.html` (categorised TOC + persona panel at top) |
| Every HTTP endpoint, spec contract | `docs/openapi/smartload-v1.yaml` + SOT §26 |
| Every Redis channel | `docs/redis-channels.md` + SOT §11.5 + SOT §28 |
| SDK reference, every method | SOT §27 |
| Operator UI architecture + workflow | SOT §28 |
| Webhooks specification | SOT §29 |
| Database design consolidation | SOT §30 |
| Per-feature manifests | `docs/features/<feature>.md` (today: `policy-management.md`) |
| Feature manifest template | `docs/features/README.md` |
| Distribution shapes + operator/tenant boundary | SOT §25 |

---

*End of walkthrough.*
