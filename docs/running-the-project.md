# How to Run SmartLoad

This document is the **living runbook** for the project. Every time a new service or feature is implemented, this file must be updated to reflect the new running instructions and any parallel processes required.

> **Rule**: If you implement something that changes how the system is started, tested, or verified — update this file in the same PR.

---

## Prerequisites

Install these before anything else:

| Tool | Purpose | Download |
|---|---|---|
| Docker Desktop | Runs all containers | https://www.docker.com/products/docker-desktop |
| Git | Version control | https://git-scm.com |
| Python 3.11+ | Local scripts and venv | https://www.python.org |
| curl or Postman | Manual endpoint testing | Built-in on Mac/Linux; use Git Bash on Windows |

**Activate the Python virtual environment** (run once per terminal session):
```powershell
# Windows PowerShell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
& .venv\Scripts\Activate.ps1

# Mac / Linux
source .venv/bin/activate
```

---

## Environment Setup

Copy the environment variable template and fill in any required values:
```bash
cp config/.env.example .env
```

Currently `.env` is not required to run the baseline stack — all defaults are built into the containers. This will change once TimescaleDB, Redis, and cloud credentials are needed.

---

## Phase 1 — Baseline Stack (Sprints 1–3) ✅ CURRENT

**What runs:** NGINX load balancer + Node.js test backends + Locust traffic simulator.

**Start:**
```bash
docker compose up --build -d
```

**Scale the backend pool** (simulate multiple servers):
```bash
docker compose up --build -d --scale test-backend=3
```

**Stop:**
```bash
docker compose down
```

### Verify it works

```bash
# Load balancer is reachable
curl http://localhost:8080

# Backend health check (through NGINX)
curl http://localhost:8080/health

# Locust web UI (traffic simulator)
open http://localhost:8089        # Mac
start http://localhost:8089       # Windows
```

Expected responses:
- `GET /` → `Hello from <container-id>`
- `GET /health` → `{"status":"healthy","server":"<container-id>"}`
- Locust UI → configure number of users and spawn rate, then click Start

### Useful commands

```bash
# View live logs from all containers
docker compose logs -f

# View logs from one service only
docker compose logs -f load-balancer
docker compose logs -f test-backend

# Check running containers and ports
docker compose ps

# Restart a single service without rebuilding
docker compose restart load-balancer

# Rebuild and restart one service only
docker compose up --build -d load-balancer
```

### Simulate backend failures (for testing)

The test backend supports env-var-controlled failure modes. Use these to test NGINX health check behaviour:

```bash
# Make one backend respond slowly (500ms delay)
docker compose run -d -e RESPONSE_DELAY_MS=500 --name slow-backend test-backend

# Make one backend return 503 on all requests
docker compose run -d -e FAIL_ALL=true --name failing-backend test-backend

# Make one backend fail health checks only
docker compose run -d -e FAIL_HEALTH=true --name unhealthy-backend test-backend
```

---

## Phase 2 — Telemetry Pipeline (Sprint 3) 🔲 NOT YET IMPLEMENTED

**What gets added:** TimescaleDB (metrics store) + OpenTelemetry Collector + Prometheus + Grafana.
These run **in parallel** with the Phase 1 stack — they don't replace anything.

**When implemented, update `docker-compose.yml` to add:**
```yaml
  timescaledb:
    image: timescale/timescaledb:latest-pg16
    environment:
      POSTGRES_PASSWORD: ${TIMESCALEDB_PASSWORD}
      POSTGRES_DB: smartloaddb
    ports:
      - "5432:5432"
    volumes:
      - ./infrastructure/timescaledb/init.sql:/docker-entrypoint-initdb.d/init.sql

  otel-collector:
    image: otel/opentelemetry-collector-contrib:latest
    volumes:
      - ./infrastructure/otel-collector/otelcol-config.yaml:/etc/otelcol-contrib/config.yaml
    ports:
      - "4317:4317"   # OTLP gRPC
      - "4318:4318"   # OTLP HTTP
    depends_on:
      - timescaledb

  prometheus:
    image: prom/prometheus:latest
    volumes:
      - ./infrastructure/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml
    ports:
      - "9090:9090"

  grafana:
    image: grafana/grafana:latest
    ports:
      - "3000:3000"
    volumes:
      - ./infrastructure/grafana/dashboards:/var/lib/grafana/dashboards
    depends_on:
      - prometheus
      - timescaledb
```

**Start (full stack with telemetry):**
```bash
docker compose up --build -d
```

**Verify telemetry:**
```bash
# TimescaleDB is reachable
psql -h localhost -U postgres -d smartloaddb

# Prometheus targets are healthy
open http://localhost:9090/targets

# Grafana dashboards
open http://localhost:3000   # default login: admin / admin
```

**`.env` additions needed:**
```
TIMESCALEDB_PASSWORD=yourpassword
```

---

## Phase 3 — Anomaly Detection (Sprint 5) 🔲 NOT YET IMPLEMENTED

**What gets added:** `anomaly-detector` service + Redis control bus.
Runs **in parallel** with Phase 1 + Phase 2. Anomaly signals are published to Redis and consumed by the load balancer.

**When implemented, update `docker-compose.yml` to add:**
```yaml
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - ./infrastructure/redis/redis.conf:/usr/local/etc/redis/redis.conf

  anomaly-detector:
    build:
      context: ./services/anomaly-detector
    environment:
      - TIMESCALEDB_URL=postgresql://postgres:${TIMESCALEDB_PASSWORD}@timescaledb:5432/smartloaddb
      - REDIS_URL=redis://redis:6379
      - POLL_INTERVAL_SECONDS=5
    depends_on:
      - timescaledb
      - redis
    restart: unless-stopped
```

**Start:**
```bash
docker compose up --build -d
```

**Verify anomaly detection:**
```bash
# Monitor anomaly signals on the Redis control bus
docker compose exec redis redis-cli subscribe smartload.anomaly

# In another terminal, trigger a slow backend
docker compose run -d -e RESPONSE_DELAY_MS=2000 --name slow-backend test-backend

# Watch the anomaly-detector logs
docker compose logs -f anomaly-detector
```

Expected: within a few monitoring intervals, the anomaly detector should publish a `DEGRADED` or `UNHEALTHY` signal for the slow backend to the `smartload.anomaly` Redis channel.

**`.env` additions needed:**
```
REDIS_URL=redis://localhost:6379
```

---

## Phase 4 — Forecasting & Autoscaler (Sprint 6) 🔲 NOT YET IMPLEMENTED

**What gets added:** `forecasting` service + `autoscaler` service.
Both run **in parallel** with the existing stack. Forecasting publishes predictions to Redis; the autoscaler reads them and scales the backend pool.

**When implemented, update `docker-compose.yml` to add:**
```yaml
  forecasting:
    build:
      context: ./services/forecasting
    environment:
      - TIMESCALEDB_URL=postgresql://postgres:${TIMESCALEDB_PASSWORD}@timescaledb:5432/smartloaddb
      - REDIS_URL=redis://redis:6379
      - FORECAST_HORIZON_MINUTES=5
    depends_on:
      - timescaledb
      - redis
    restart: unless-stopped

  autoscaler:
    build:
      context: ./services/autoscaler
    environment:
      - REDIS_URL=redis://redis:6379
      - DOCKER_HOST=unix:///var/run/docker.sock
      - MIN_BACKENDS=1
      - MAX_BACKENDS=5
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    depends_on:
      - redis
    restart: unless-stopped
```

**Start:**
```bash
docker compose up --build -d
```

**Verify forecasting and autoscaling:**
```bash
# Monitor forecast signals
docker compose exec redis redis-cli subscribe smartload.forecast

# Monitor scale events
docker compose exec redis redis-cli subscribe smartload.scale

# Watch autoscaler logs
docker compose logs -f autoscaler

# Watch forecasting logs
docker compose logs -f forecasting
```

Expected: as Locust generates load, the forecasting service should publish `next_5_min_request_rate` values to `smartload.forecast`. The autoscaler reads these and launches or terminates `test-backend` containers accordingly.

---

## Phase 5 — Reinforcement Learning Engine (Sprint 7) 🔲 NOT YET IMPLEMENTED

**What gets added:** `rl-engine` service — runs in **shadow mode** initially (logs routing recommendations without applying them), then promoted to active routing.

**When implemented, update `docker-compose.yml` to add:**
```yaml
  rl-engine:
    build:
      context: ./services/rl-engine
    environment:
      - TIMESCALEDB_URL=postgresql://postgres:${TIMESCALEDB_PASSWORD}@timescaledb:5432/smartloaddb
      - REDIS_URL=redis://redis:6379
      - RL_MODE=shadow          # shadow | active
      - MODEL_PATH=/models/policy.zip
    volumes:
      - ./services/rl-engine/models:/models
    depends_on:
      - timescaledb
      - redis
    restart: unless-stopped
```

**Start:**
```bash
docker compose up --build -d
```

**Verify RL engine (shadow mode):**
```bash
# Monitor routing recommendations (shadow — not applied yet)
docker compose exec redis redis-cli subscribe smartload.routing

# Watch RL engine logs
docker compose logs -f rl-engine
```

**Promote RL to active routing** (once shadow mode is validated):
```bash
# Update RL_MODE in docker-compose.yml or .env:
RL_MODE=active

docker compose up -d rl-engine   # restart with new config
```

Expected in shadow mode: the RL engine should publish server ranking scores to `smartload.routing` every few seconds without affecting live traffic. In active mode, the load balancer reads these scores and routes accordingly.

---

## Phase 6 — Policy Manager (Sprint 8) 🔲 NOT YET IMPLEMENTED

**What gets added:** `policy-manager` service — central REST API for operator control.

**When implemented, update `docker-compose.yml` to add:**
```yaml
  policy-manager:
    build:
      context: ./services/policy-manager
    environment:
      - REDIS_URL=redis://redis:6379
      - CONFIG_PATH=/config/policy.yaml
    volumes:
      - ./config/policy.yaml:/config/policy.yaml
    ports:
      - "8086:8086"
    depends_on:
      - redis
    restart: unless-stopped
```

**Start:**
```bash
docker compose up --build -d
```

**Verify policy manager:**
```bash
# Get current policy
curl http://localhost:8086/api/v1/policy

# Enable safe mode (disable RL, force classical routing)
curl -X POST http://localhost:8086/api/v1/policy \
  -H "Content-Type: application/json" \
  -d '{"safe_mode": true}'

# Disable safe mode (re-enable RL)
curl -X POST http://localhost:8086/api/v1/policy \
  -H "Content-Type: application/json" \
  -d '{"safe_mode": false}'

# Set SLO target
curl -X POST http://localhost:8086/api/v1/policy \
  -H "Content-Type: application/json" \
  -d '{"slo_p95_latency_ms": 200}'
```

---

## Full Stack Reference (All Phases Complete)

When all services are implemented, the full running stack will be:

```
Port 8080  →  load-balancer        (NGINX ingress)
Port 8086  →  policy-manager       (REST API)
Port 8089  →  traffic-simulator    (Locust UI)
Port 3000  →  grafana              (Dashboards)
Port 9090  →  prometheus           (Metrics)
Port 5432  →  timescaledb          (Metrics DB)
Port 6379  →  redis                (Control bus)
Port 4317  →  otel-collector       (OTLP gRPC)
Port 4318  →  otel-collector       (OTLP HTTP)

Internal only:
           →  test-backend         (dummy backends, scaled N instances)
           →  telemetry            (OTel pipeline)
           →  anomaly-detector     (anomaly signals)
           →  forecasting          (load predictions)
           →  rl-engine            (routing decisions)
           →  autoscaler           (scaling actions)
```

**Start everything:**
```bash
docker compose up --build -d
```

**Monitor all services at once:**
```bash
# All logs
docker compose logs -f

# All Redis control bus channels
docker compose exec redis redis-cli psubscribe 'smartload.*'

# Grafana dashboards
open http://localhost:3000
```

---

## Running Tests

**Unit tests** (no Docker required — runs against local Python):
```bash
pytest tests/unit/ -v
```

**Integration tests** (requires the full stack to be running):
```bash
docker compose up -d
pytest tests/integration/ -v
docker compose down
```

**Performance / load test** (Locust):
```bash
# Option 1 — use the Locust web UI
open http://localhost:8089

# Option 2 — headless mode (100 users, 10/s spawn rate, run for 60s)
docker compose exec traffic-simulator \
  locust --headless -u 100 -r 10 --run-time 60s --host http://load-balancer
```

---

## Troubleshooting

**Docker daemon not running:**
```bash
# Start Docker Desktop first, then retry
docker compose up --build -d
```

**Port already in use:**
```bash
# Find what is using the port (e.g., 8080)
netstat -ano | findstr :8080       # Windows
lsof -i :8080                      # Mac / Linux

# Stop all compose containers and retry
docker compose down
docker compose up --build -d
```

**Container keeps restarting:**
```bash
# Check the logs for the failing container
docker compose logs <service-name>

# Example
docker compose logs load-balancer
```

**NGINX returns 502 Bad Gateway:**
- The `test-backend` container is not running or not healthy
- Check: `docker compose ps` — test-backend should show `Up`
- Check: `docker compose logs test-backend`

**Rebuild from scratch** (clears all images and volumes):
```bash
docker compose down --volumes --rmi all
docker compose up --build -d
```

---

## Quick Reference Card

| Task | Command |
|---|---|
| Start stack | `docker compose up --build -d` |
| Start with 3 backends | `docker compose up --build -d --scale test-backend=3` |
| Stop stack | `docker compose down` |
| View all logs | `docker compose logs -f` |
| View one service logs | `docker compose logs -f <service>` |
| Restart one service | `docker compose restart <service>` |
| Check container status | `docker compose ps` |
| Test load balancer | `curl http://localhost:8080` |
| Test health endpoint | `curl http://localhost:8080/health` |
| Open Locust UI | `http://localhost:8089` |
| Open Grafana | `http://localhost:3000` |
| Full reset | `docker compose down --volumes --rmi all` |
