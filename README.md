# SmartLoad

AI-driven intelligent load management middleware for distributed systems.
SmartLoad sits between client traffic and backend services, combining classical load balancing with reinforcement learning, predictive autoscaling, and anomaly detection.

> **Academic project** — Zewail City of Science, Technology, and Innovation | CIE Graduation Project 2025/2026

---

## System Overview

SmartLoad is a modular middleware platform that manages how HTTP requests are routed across a backend server pool. It collects real-time telemetry, detects unhealthy nodes, forecasts future load, and applies adaptive routing decisions — all without replacing existing infrastructure.

**Eight authored microservices:**

| # | Service | Role |
|---|---------|------|
| 1 | `load-balancer` | Traffic ingress; classical and AI-driven routing |
| 2 | `lb-otel-shipper` | Sidecar — tails NGINX JSON access log, emits per-request OTLP |
| 3 | `telemetry` | OTLP/HTTP-JSON ingress → TimescaleDB; read API for engines |
| 4 | `anomaly-detector` | Real-time detection of unhealthy backend nodes |
| 5 | `forecasting` | Short-term workload prediction for proactive scaling |
| 6 | `rl-engine` | Reinforcement learning routing decisions (PPO) |
| 7 | `autoscaler` | Proactive resource scaling (Docker / K8s / AWS) |
| 8 | `policy-manager` | Central governance, safe-mode, SLO enforcement |

**Five configured infrastructure components** (not authored by the team):
TimescaleDB · Redis (control bus) · OTel Collector · Prometheus · Grafana

---

## Architecture

```
Client Traffic
      |
      v
services/load-balancer        ← NGINX ingress; round-robin upstream
      |  └── writes JSON access log → shared nginx-logs volume
      |        └── services/lb-otel-shipper (sidecar, tails the log)
      |              └── OTLP/HTTP-JSON per request → otel-collector → telemetry
      |
      |──── services/telemetry           (OTLP ingress → TimescaleDB; read API)
      |──── services/anomaly-detector    (flags unhealthy nodes → control bus)
      |──── services/forecasting         (predicts load → control bus)
      |──── services/rl-engine           (PPO routing scores → control bus)
      |──── services/policy-manager      (safe-mode, SLOs, scaling limits)
      |
      v
services/autoscaler           ← scales test-backends up/down
      |
      v
test-backends                 ← dummy Node.js Express backends (testing only)
```

**Routing decision hierarchy** (always applied in this order):
1. Exclude nodes flagged `UNHEALTHY` by `anomaly-detector`
2. Apply RL routing scores from `rl-engine` (if enabled and confident)
3. Fall back to classical algorithm (Round Robin / Least Connections)

---

## Repository Structure

This structure is the **team's canonical reference**. Do not create new top-level folders.
Every file type has exactly one correct location, described below.

```
smartload/
│
├── services/                    ← ALL SmartLoad-authored microservices
│   ├── shared/                  ← Cross-service canonical contracts (only allowed cross-import)
│   │   ├── contracts.py         ←   Redis pub/sub envelope + payload dataclasses
│   │   └── queries.py           ←   TimescaleDB SQL constants (METRICS_INSERT, ANOMALY_QUERY, …)
│   ├── load-balancer/           ← Traffic ingress service
│   │   ├── nginx/               ← NGINX implementation
│   │   │   ├── nginx.conf       ←   routing rules, upstream pool, JSON logging
│   │   │   └── Dockerfile
│   │   └── .gitkeep             ← Placeholder for future routing-sidecar (T2.1)
│   ├── lb-otel-shipper/         ← NGINX log → OTLP/HTTP-JSON shipper (T1.2)
│   │   ├── app.py               ←   Tail-and-emit Python loop (~250 LOC)
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── telemetry/               ← OTLP/HTTP-JSON ingress + read API + stats counters
│   │   ├── app.py
│   │   ├── Dockerfile
│   │   └── config/              ← Telemetry-service tunables (currently unused)
│   ├── anomaly-detector/        ← Anomaly detection engine (Isolation Forest, LSTM)
│   │   ├── app.py
│   │   ├── Dockerfile
│   │   ├── models/              ← Trained model weights (.pkl, .pt) go here
│   │   └── config/              ← Detection thresholds and model selection config
│   ├── forecasting/             ← Workload forecasting (ARIMA, Prophet, LSTM)
│   │   ├── app.py
│   │   ├── Dockerfile
│   │   ├── models/              ← Trained forecasting model artifacts go here
│   │   └── config/              ← Model selection and horizon config
│   ├── rl-engine/               ← RL decision engine (PPO via Stable-Baselines3)
│   │   ├── app.py
│   │   ├── Dockerfile
│   │   ├── models/              ← PPO policy checkpoints (.zip) go here
│   │   └── config/              ← Hyperparameters, environment config
│   ├── autoscaler/              ← Resource manager (Docker / K8s / AWS)
│   │   ├── app.py
│   │   ├── Dockerfile
│   │   └── config/              ← Scaling thresholds, cooldown periods, provider config
│   └── policy-manager/          ← Central policy and safe-mode API
│       ├── app.py
│       ├── Dockerfile
│       └── config/              ← Service-level policy overrides
│
├── test-backends/               ← Dummy backends (Node.js Express) — testing only
│   ├── app.js                   ← Supports RESPONSE_DELAY_MS, FAIL_ALL, FAIL_HEALTH
│   ├── Dockerfile
│   ├── package.json
│   └── package-lock.json
│
├── infrastructure/              ← Config for 3rd-party infra components ONLY
│   │                               (no team-authored application code goes here)
│   ├── grafana/                 ← Grafana dashboard JSON and provisioning config
│   ├── k8s/                     ← Kubernetes manifests (deployments, services, ingress)
│   ├── otel-collector/          ← OpenTelemetry Collector config (otelcol-config.yaml)
│   ├── prometheus/              ← Prometheus scrape config (prometheus.yml)
│   ├── redis/                   ← Redis config (redis.conf); Redis is the control bus
│   └── timescaledb/             ← DB init SQL, schema migrations
│
├── datasets/                    ← Raw training and evaluation data (one folder per source)
│   ├── borg/                    ← Google Borg cluster traces → RL training, Forecasting
│   ├── alibaba/                 ← Alibaba microservice traces → RL, Forecasting, Anomaly
│   ├── nab/                     ← Numenta Anomaly Benchmark → Anomaly Detection
│   └── yahoo-smd/               ← Yahoo Server Machine Dataset → Anomaly Detection
│
├── tests/                       ← All test suites
│   ├── unit/                    ← Per-module unit tests (pytest)
│   ├── integration/             ← Cross-service integration tests (pytest + compose stack)
│   └── performance/             ← Load and stress tests
│       ├── locustfile.py        ←   Locust traffic simulation
│       └── Dockerfile           ←   Container for running Locust
│
├── docs/                        ← Human-readable technical documentation
│   └── SOURCE_OF_TRUTH.html     ← Single canonical SoT — design contracts, diagrams, roadmap
│
├── config/                      ← System-wide shared configuration
│   ├── policy.yaml              ← Global SmartLoad policy (routing mode, SLOs, limits)
│   └── .env.example             ← Template for all required environment variables
│
├── scripts/                     ← Developer and ops utility scripts
│   ├── setup.sh                 ← Bootstrap local dev environment
│   ├── seed-metrics.py          ← Synthetic metrics seeder (N1.x development)
│   └── download-datasets.sh     ← Fetch and verify public datasets
│
├── docker-compose.yml           ← Run the full dev stack from the repo root
├── .gitignore
├── LICENSE
└── README.md
```

### Placement Rules

These rules eliminate ambiguity. When in doubt, follow these:

| File type | Where it goes |
|---|---|
| New SmartLoad microservice | `services/<service-name>/` with its own `Dockerfile` |
| Trained ML model weights | `services/<service-name>/models/` — never in `datasets/` |
| Service-specific config (thresholds, hyperparams) | `services/<service-name>/config/` |
| 3rd-party infra config (Grafana dashboard, prometheus.yml) | `infrastructure/<tool-name>/` |
| Raw dataset files | `datasets/<source-name>/` |
| Dataset preprocessing scripts | `scripts/` |
| Unit test for a service | `tests/unit/` |
| Integration test | `tests/integration/` |
| Load test | `tests/performance/` |
| Global policy (safe_mode, SLO targets) | `config/policy.yaml` |
| Secret environment variables | `config/.env.example` (template); `.env` at root (gitignored) |
| Kubernetes manifests | `infrastructure/k8s/` |

---

## Quick Start

```bash
# 1. Clone the repository
git clone <repo-url> && cd smartload

# 2. Set up environment variables
cp config/.env.example .env
# Edit .env and fill in required values

# 3. Start the full stack
docker compose up --build
```

Services will be available at:
- **Load balancer**: `http://localhost:8080`
- **Locust UI** (traffic simulator): `http://localhost:8089`

Scale the backend pool for testing:
```bash
docker compose up --build --scale test-backend=3
```

---

## Services Reference

| Service | Language | Port | Key Tech | Dockerfile |
|---|---|---|---|---|
| `load-balancer` | NGINX | 8080 | Round Robin, health checks, JSON access log | `services/load-balancer/nginx/Dockerfile` |
| `lb-otel-shipper` | Python | — (sidecar) | Log tail loop → OTLP/HTTP-JSON | `services/lb-otel-shipper/Dockerfile` |
| `telemetry` | Python | 8081 | Flask + psycopg2 + TimescaleDB; OTLP ingress + read API | `services/telemetry/Dockerfile` |
| `anomaly-detector` | Python | 8082 | Isolation Forest, LSTM autoencoder | `services/anomaly-detector/Dockerfile` |
| `forecasting` | Python | 8083 | ARIMA, Prophet, LSTM | `services/forecasting/Dockerfile` |
| `rl-engine` | Python | 8084 | PPO via Stable-Baselines3 | `services/rl-engine/Dockerfile` |
| `autoscaler` | Python | 8085 | Docker API, K8s API, Boto3 | `services/autoscaler/Dockerfile` |
| `policy-manager` | Python | 8086 | YAML config + REST API | `services/policy-manager/Dockerfile` |

**Telemetry HTTP API** (`services/telemetry/app.py`):
| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/metrics` | OTLP/HTTP-JSON ingress (collector forwards here; always 200) |
| GET | `/api/v1/metrics?service=&window=` | Recent rows; `window` accepts `30s` / `5m` / `1h` / `2d` |
| GET | `/api/v1/stats` | `rows_written` / `batches_written` / `rows_dropped_db` / `rows_dropped_shape` |
| GET | `/health` | 200 if Redis + TimescaleDB reachable; 503 otherwise |

---

## Infrastructure Components

| Component | Purpose | Config location |
|---|---|---|
| **TimescaleDB** | Time-series metrics store (PostgreSQL extension) | `infrastructure/timescaledb/` |
| **Redis** | Async control bus (Pub/Sub between decision modules) | `infrastructure/redis/` |
| **Prometheus** | Metrics scraping from all services | `infrastructure/prometheus/` |
| **Grafana** | Dashboards for latency, throughput, anomalies | `infrastructure/grafana/` |
| **OTel Collector** | Aggregates and forwards telemetry to TimescaleDB | `infrastructure/otel-collector/` |

**Control bus channels** (Redis Pub/Sub):
- `smartload.anomaly` — health signals from anomaly-detector
- `smartload.forecast` — load predictions from forecasting
- `smartload.routing` — server rankings from rl-engine
- `smartload.scale` — scale-up/down events from autoscaler
- `smartload.policy` — policy updates from policy-manager

---

## Testing

**Unit tests** — test individual service logic in isolation:
```bash
pytest tests/unit/
```

**Integration tests** — test the full compose stack end-to-end:
```bash
docker compose up -d
pytest tests/integration/
docker compose down
```

**Performance / load tests** — run Locust traffic simulation:
```bash
docker compose up --build
# Open http://localhost:8089 and configure load parameters
```
Or run Locust directly:
```bash
docker compose run --rm traffic-simulator
```

---

## Datasets

All datasets are publicly available. Raw files go in `datasets/<source>/`; use `scripts/download-datasets.sh` to fetch them.

| Dataset | Directory | Used by | License |
|---|---|---|---|
| Google Borg Cluster Traces | `datasets/borg/` | rl-engine, forecasting | CC-BY |
| Alibaba Microservice Traces | `datasets/alibaba/` | rl-engine, forecasting, anomaly-detector | Open |
| Numenta Anomaly Benchmark (NAB) | `datasets/nab/` | anomaly-detector | MIT |
| Yahoo Server Machine Dataset (SMD) | `datasets/yahoo-smd/` | anomaly-detector | Open |

---

## Configuration

**Global policy** (`config/policy.yaml`):
Controls system-wide behavior — operating mode (`classical` / `hybrid` / `learning`), `safe_mode` flag (disables RL), SLO P95 latency target, autoscaler min/max instance limits.

**Environment variables** (`config/.env.example`):
Copy to `.env` at the repo root and fill in values. Contains database credentials, Redis connection strings, and cloud provider keys. The `.env` file is gitignored and must never be committed.

**Service-specific config** (`services/<name>/config/`):
Each service reads its own tuning config from this directory — model hyperparameters, detection thresholds, scaling policies. Changes here do not require a rebuild if the service reads the file at runtime.

---

## CI/CD

Pipeline defined in `.github/workflows/docker-publish.yml`. Triggers on push and PR to `main`.

| Job | What it does |
|---|---|
| `lint` | Runs `ruff check` on all Python in `services/` and `test-backends/` |
| `unit-tests` | Pure-Python tests (parser shape, envelope, contracts, SQL parameterisation) |
| `build-services` | Matrix build for all 7 Python services (incl. `lb-otel-shipper`); health-checks each container |
| `build-test-backend` | Builds and health-checks the Node.js dummy backend |
| `compose-test` | Spins up the full stack (`docker compose up`), runs Phase-0 wiring + S2 baseline + T1.1 ingest + T1.2 fidelity tests against the live stack |

The `docker-compose.yml` is at the repo root — no `-f` flag needed.

---

## Academic Context

`docs/SOURCE_OF_TRUTH.html` is the canonical project specification — design contracts per service, the wired channel/REST/SQL surfaces, system diagrams, sprint roadmap, and component build status. Every code change must keep this document aligned.

Project supervisors: Dr. Tamer Ashour (CIE) · Dr. Doaa Shawky (Software Dev Program)

---

## License

This project is developed as part of an academic research project at Zewail City of Science, Technology, and Innovation.
