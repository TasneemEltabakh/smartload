# SmartLoad — Team Task Plan

> **Rule:** Update this file in the same PR when a task is completed. Check the box `[x]`, update the status table, and keep the dependency graph accurate.
> This file is the team's single source of truth for what to work on next.

---

## Principles

1. **Integration-first** — wire before you implement. Every service must connect to Redis and TimescaleDB before any ML logic is added.
2. **No service goes dark** — even a stub must publish a heartbeat or health signal to the control bus so the rest of the system knows it exists.
3. **One branch per task** — branch name format: `feat/<task-id>-short-description`. PR to `main` when done.
4. **Living docs** — update `docs/running-the-project.md` in the same PR as any service that changes how the stack starts or stops.
5. **Cross-team coordination** — Nada's schema requirements (N0.2) must be reviewed before Tasneem finalizes the DB schema (T0.2). Block the PR until both sign off.

---

## Current Status

Last updated: **24 April 2026** — Phase 0 Integration Foundation complete (T0.1–T0.8, N0.1–N0.3)

| Component | Owner | Status |
|---|---|---|
| NGINX load balancer | Tasneem | ✅ Done |
| test-backends (Node.js Express) | Tasneem | ✅ Done |
| Locust traffic simulator | Tasneem | ✅ Done |
| CI/CD pipeline (GitHub Actions) | Tasneem | ✅ Done |
| Repo structure + runbook | Tasneem | ✅ Done |
| docker-compose (full stack) | Tasneem | ✅ Done (13 services) |
| TimescaleDB schema | Tasneem | ✅ Done (init.sql, 3 hypertables) |
| OTel Collector config | Tasneem | ✅ Done |
| Redis config | Tasneem | ✅ Done |
| Prometheus config | Tasneem | ✅ Done |
| Grafana dashboards | Tasneem | 🟡 Provisioning done; dashboards pending |
| config/.env.example | Tasneem | ✅ Done |
| Telemetry service (wired) | Tasneem | ✅ Done (connected, real logic Phase 1) |
| Anomaly detector (wired) | Nada | ✅ Done (connected, real logic Phase 1) |
| Forecasting (wired) | Nada | ✅ Done (connected, real logic Phase 1) |
| RL engine (wired) | Nada | ✅ Done (connected, real logic Phase 1) |
| Autoscaler (wired) | Tasneem | ✅ Done (connected, real logic Phase 1) |
| Policy manager (wired) | Tasneem | ✅ Done (REST API + Redis publish) |
| Redis message contracts | Nada | ✅ Done (services/shared/contracts.py) |
| TimescaleDB query interfaces | Nada | ✅ Done (services/shared/queries.py) |
| Dataset download scripts | Rghda | 🔲 Not started |
| Google Borg dataset | Rghda | 🔲 Not started |
| Alibaba dataset | Rghda | 🔲 Not started |
| NAB dataset | Rghda | 🔲 Not started |
| Yahoo SMD dataset | Rghda | 🔲 Not started |
| Data preprocessors | Rghda | 🔲 Not started |
| Data loader utilities | Rghda | 🔲 Not started |
| Anomaly detection model | Nada | 🔲 Not started |
| Forecasting model | Nada | 🔲 Not started |
| RL training environment | Nada | 🔲 Not started |
| Integration tests | All | 🔲 Not started |
| Unit tests | All | 🔲 Not started |

---

## Phase 0 — Integration Foundation

**Goal:** Full docker-compose stack starts cleanly. Every service connects to Redis and TimescaleDB on startup and logs the connection result — even if it does nothing else.

**Deadline:** End of April 2026

**All three work in parallel. T0.6 is the critical gate — nothing can be integration-tested until the full compose stack is up.**

---

### Tasneem — Infrastructure Wiring

> Do these in order. T0.6 depends on T0.2–T0.5 being written first.

- [x] **T0.1** — Populate `config/.env.example`
  - Add every env var the full stack needs: `TIMESCALEDB_URL`, `TIMESCALEDB_PASSWORD`, `REDIS_URL`, service ports, OTel endpoint
  - Use placeholder values (e.g., `TIMESCALEDB_PASSWORD=changeme`)

- [x] **T0.2** — Write `infrastructure/timescaledb/init.sql`
  - Create TimescaleDB extension
  - Create hypertables:
    ```sql
    metrics(time TIMESTAMPTZ, service TEXT, instance TEXT, metric_name TEXT, value DOUBLE PRECISION)
    backend_health(time TIMESTAMPTZ, backend_id TEXT, status TEXT, score DOUBLE PRECISION)
    scaling_events(time TIMESTAMPTZ, action TEXT, instance_count INT, reason TEXT)
    ```
  - ⚠️ **Block this PR until N0.3 confirms the schema matches AI query needs**

- [x] **T0.3** — Write `infrastructure/redis/redis.conf`
  - Disable persistence for dev (`save ""`)
  - Set `maxmemory-policy allkeys-lru`
  - Set `maxmemory 256mb`

- [x] **T0.4** — Write `infrastructure/otel-collector/otelcol-config.yaml`
  - Receivers: `otlp` (gRPC port 4317, HTTP port 4318)
  - Exporters: `prometheus` (port 8889) + `otlphttp` to TimescaleDB adapter
  - Pipeline: metrics → batch → prometheus + otlphttp

- [x] **T0.5** — Write `infrastructure/prometheus/prometheus.yml`
  - Global scrape interval: 15s
  - Scrape targets: OTel Collector (8889), all 7 services (ports 8081–8087)

- [x] **T0.6** — Expand `docker-compose.yml` to the full stack
  - Add infrastructure services:
    ```
    timescaledb   (timescale/timescaledb:latest-pg16, port 5432)
    redis         (redis:7-alpine, port 6379)
    otel-collector (otel/opentelemetry-collector-contrib, ports 4317/4318)
    prometheus    (prom/prometheus, port 9090)
    grafana       (grafana/grafana, port 3000)
    ```
  - Add all 6 SmartLoad services (telemetry, anomaly-detector, forecasting, rl-engine, autoscaler, policy-manager)
  - Each service gets: `TIMESCALEDB_URL`, `REDIS_URL` env vars
  - Each service `depends_on: [timescaledb, redis]` with `condition: service_healthy`
  - Add healthchecks to timescaledb and redis

- [x] **T0.7** — Update all 6 service stubs to connect on startup
  - For each service (`telemetry`, `anomaly-detector`, `forecasting`, `rl-engine`, `autoscaler`, `policy-manager`):
    - Add `requirements.txt`: `psycopg2-binary`, `redis`
    - Update `Dockerfile` to `COPY requirements.txt .` + `RUN pip install -r requirements.txt`
    - Update `app.py` to:
      - Read `TIMESCALEDB_URL` and `REDIS_URL` from env
      - On startup: attempt DB connection, log `[timescaledb] connected` or `[timescaledb] connection failed: <error>`
      - On startup: attempt Redis connection, log `[redis] connected` or `[redis] connection failed: <error>`
      - Update `/health` to return `{"status": "ok", "timescaledb": true/false, "redis": true/false}`

- [x] **T0.8** — Update `docs/running-the-project.md` Phase 2 section with the real compose config

**Definition of done:** `docker compose up --build` starts all 11+ services without errors. Each service `/health` endpoint returns `{"timescaledb": true, "redis": true}`.

---

### Nada — AI Contracts

> These run in parallel with Tasneem. N0.3 must happen before T0.2 is merged.

- [x] **N0.1** — Define Redis message schemas
  - Create `services/shared/contracts.py` with typed dataclasses:
    ```python
    @dataclass
    class AnomalyEvent:
        backend_id: str
        status: str          # "healthy" | "degraded" | "unhealthy"
        score: float
        timestamp: str       # ISO 8601

    @dataclass
    class ForecastResult:
        horizon_minutes: int
        predicted_rps: float
        confidence_lower: float
        confidence_upper: float
        timestamp: str

    @dataclass
    class RoutingRecommendation:
        mode: str            # "shadow" | "active"
        server_rankings: list[dict]   # [{backend_id, score}]
        timestamp: str
    ```
  - Add `json_encode()` / `json_decode()` helpers
  - All services import from this shared file

- [x] **N0.2** — Define TimescaleDB query requirements
  - Write the exact SQL queries each AI service needs as constants in `services/shared/queries.py`:
    - Anomaly detector: last N minutes of latency + error rate per backend
    - Forecasting: last M hours of request_rate time series
    - RL engine: current load, latency, health per backend
  - These queries define what columns must exist in T0.2's schema

- [x] **N0.3** — Review T0.2 schema PR and verify it satisfies N0.2
  - If columns are missing or types are wrong: comment on the PR before it merges

---

### Rghda — Dataset Acquisition

> These run in parallel with Tasneem and Nada.

- [ ] **R0.1** — Write `scripts/download-datasets.sh`
  - NAB: `git clone https://github.com/numenta/NAB datasets/nab`
  - Google Borg: wget from GCS public bucket (`gs://clusterdata-2011-2`) into `datasets/borg/`
  - Alibaba: document manual download steps from https://github.com/alibaba/clusterdata in `datasets/alibaba/README.md`
  - Yahoo SMD: document manual download steps (requires Yahoo registration) in `datasets/yahoo-smd/README.md`

- [ ] **R0.2** — Download NAB and Google Borg
  - Run `scripts/download-datasets.sh` and confirm files land in `datasets/nab/` and `datasets/borg/`
  - Verify file sizes and spot-check a few rows

- [ ] **R0.3** — Write `datasets/<name>/README.md` for each dataset
  - Columns and data types
  - Time range and total size
  - Which SmartLoad module uses it (anomaly-detector, forecasting, rl-engine)
  - Any known quality issues (missing values, format quirks)

- [ ] **R0.4** — Coordinate with Nada on N0.2
  - Confirm the columns available in each dataset match what the AI queries expect
  - If there's a mismatch: agree on a preprocessing step that bridges the gap

---

## Phase 1 — Service Skeletons (Real Logic, Minimal)

**Goal:** Each service does something real that flows through the full stack — not ML yet, but real queries, real Redis messages, and real responses.

**Prerequisite:** Phase 0 complete (T0.7 done — all services connect to Redis + TimescaleDB).

**Deadline:** Mid May 2026

---

### Tasneem

- [ ] **T1.1 — Telemetry service: real OTel → TimescaleDB pipeline**
  - Receive OTLP metrics pushed by OTel Collector
  - Write rows to `metrics` hypertable (psycopg2)
  - Expose `GET /api/v1/metrics?service=load-balancer&window=5m` returning recent metric rows as JSON

- [ ] **T1.2 — NGINX OTel instrumentation**
  - Add `ngx_otel_module` to the load balancer Dockerfile (or use a Prometheus exporter sidecar)
  - Push `request_count`, `request_latency_ms`, `upstream_backend` labels to OTel Collector
  - Verify metrics appear in Prometheus at `http://localhost:9090`

- [ ] **T1.3 — Autoscaler skeleton**
  - Subscribe to `smartload.forecast` Redis channel
  - On each forecast message: compare `predicted_rps` to `current_backends × PER_INSTANCE_CAPACITY`
  - Scale-out: use Docker SDK (`docker.from_env()`) to start new `test-backend` containers
  - Scale-in: after cooldown period, stop excess containers
  - Publish `scaling_events` row to TimescaleDB after each action
  - Enforce `MIN_BACKENDS` and `MAX_BACKENDS` from env vars

- [ ] **T1.4 — Policy manager REST API**
  - Read `config/policy.yaml` on startup
  - `GET /api/v1/policy` → return policy as JSON
  - `POST /api/v1/policy` → update field, persist to yaml, publish update to `smartload.policy` Redis channel
  - Core policy fields: `operating_mode`, `safe_mode`, `min_backends`, `max_backends`, `slo_p95_latency_ms`, `anomaly_latency_multiplier`

- [ ] **T1.5 — Populate `config/policy.yaml`** with real defaults:
  ```yaml
  operating_mode: hybrid
  safe_mode: false
  min_backends: 1
  max_backends: 5
  slo_p95_latency_ms: 200
  anomaly_latency_multiplier: 3.0
  per_instance_capacity_rps: 100
  autoscaler_cooldown_seconds: 60
  ```

- [ ] **T1.6 — Populate `config/.env.example`** with all real variable names and example values

---

### Nada

- [ ] **N1.1 — Anomaly detector: threshold-based, real**
  - Every 5s: run the query from N0.2 to get last 60s of latency + error rate per backend
  - Compute rolling mean; if `current_latency > multiplier × rolling_mean` OR `error_rate > threshold` → flag as DEGRADED or UNHEALTHY
  - Publish `AnomalyEvent` (from contracts.py) to `smartload.anomaly` Redis channel
  - Subscribe to `smartload.policy` to dynamically read `anomaly_latency_multiplier`
  - This threshold logic is the baseline — Isolation Forest replaces it in Phase 2

- [ ] **N1.2 — Forecasting: moving average, real**
  - Every 60s: run the query from N0.2 to get last 1 hour of `request_rate`
  - Compute rolling mean as the next-5-minute forecast
  - Publish `ForecastResult` (from contracts.py) to `smartload.forecast` Redis channel
  - ARIMA/Prophet replaces this in Phase 2

- [ ] **N1.3 — RL engine: shadow-mode scaffold**
  - Every 5s: run the query from N0.2 to get current system state per backend
  - Build state vector: `[latency_per_backend, request_rate_per_backend, health_flag_per_backend]`
  - Select an action using a random policy (uniform random backend selection)
  - Publish `RoutingRecommendation` with `mode="shadow"` to `smartload.routing`
  - Log the recommendation but do not affect live routing
  - This validates the full pipeline before a trained PPO policy is used

---

### Rghda

- [ ] **R1.1 — Preprocess NAB dataset**
  - Write `datasets/nab/preprocess.py`
  - Output: `datasets/nab/processed/` — one CSV per anomaly series with columns: `timestamp`, `value`, `is_anomaly`
  - Align timestamps to UTC, normalize values to [0, 1]

- [ ] **R1.2 — Preprocess Google Borg dataset**
  - Write `datasets/borg/preprocess.py`
  - Extract CPU/memory usage per job per 5-minute interval
  - Output: `datasets/borg/processed/` — time series CSVs with columns: `timestamp`, `job_id`, `cpu_usage`, `mem_usage`

- [ ] **R1.3 — Exploratory data analysis**
  - One script or notebook per dataset showing: value distribution, anomaly frequency (NAB), time-of-day patterns (Borg), missing value counts
  - Save output plots to `datasets/<name>/eda/`
  - Share findings with Nada so she knows what edge cases to expect

- [ ] **R1.4 — Data loader utilities**
  - Write `datasets/loaders.py` — the standard interface Nada imports for training:
    ```python
    def load_nab(metric_name: str) -> pd.DataFrame:  # columns: timestamp, value, is_anomaly
    def load_borg_timeseries(resample: str = "5min") -> pd.DataFrame:  # columns: timestamp, cpu_usage, mem_usage
    ```

---

## Phase 2 — Real AI Implementation

**Goal:** Replace skeleton logic with trained models. The load balancer reads signals from the Redis control bus and dynamically updates routing.

**Prerequisite:** Phase 1 complete. Datasets preprocessed (R1.1, R1.2, R1.4 done).

**Deadline:** End of May 2026

---

### Tasneem

- [ ] **T2.1 — Load balancer reads Redis signals (dynamic routing)**
  - Run a Go or Python sidecar alongside NGINX that subscribes to:
    - `smartload.anomaly` → maintain a set of UNHEALTHY backend IDs
    - `smartload.routing` → read server scores when `mode=active`
    - `smartload.policy` → toggle `safe_mode`
  - On anomaly: rewrite NGINX upstream config (or use `nginx -s reload`) to exclude the unhealthy backend
  - On RL recommendation: set NGINX upstream weights proportional to server scores
  - On `safe_mode=true`: reset all weights to equal, stop consuming RL signals
  - Document in `docs/running-the-project.md` Phase 5 section

- [ ] **T2.2 — Grafana dashboards**
  - Create provisioned dashboards in `infrastructure/grafana/dashboards/`:
    - **Overview**: request rate, P95 latency, active backend count over time
    - **Anomaly**: anomaly events per backend (timeline), DEGRADED/UNHEALTHY durations
    - **Forecast**: predicted vs actual request rate (overlay)
    - **RL**: RL routing weights per backend vs round-robin baseline

- [ ] **T2.3 — Integration tests** (`tests/integration/`)
  - Test 1: trigger FAIL_HEALTH on a backend → anomaly detector flags it → NGINX stops routing to it within 2 monitoring intervals
  - Test 2: ramp up Locust users → forecasting predicts spike → autoscaler launches new container within 2 minutes
  - Test 3: POST `safe_mode=true` to policy manager → routing reverts to equal weights, RL signals ignored
  - Use `pytest` + `docker compose` fixture to spin up/down the stack

---

### Nada

- [ ] **N2.1 — Anomaly detector: Isolation Forest**
  - Train `IsolationForest` (scikit-learn) on preprocessed NAB + Yahoo SMD data
  - Features: `latency`, `error_rate`, `latency_rolling_mean`, `latency_rolling_std`
  - Save trained model: `services/anomaly-detector/models/isolation_forest.pkl`
  - Replace threshold logic in N1.1 with model inference
  - Load model on startup; fall back to threshold logic if model file is missing

- [ ] **N2.2 — Forecasting: ARIMA or Prophet**
  - Train ARIMA or Prophet on preprocessed Borg time series (use Rghda's `load_borg_timeseries()`)
  - Evaluate on held-out data using MAE and MAPE — report results
  - Save model: `services/forecasting/models/`
  - Replace moving average in N1.2 with model inference
  - Keep moving average as fallback if model file is missing

- [ ] **N2.3 — RL training environment**
  - Build a custom OpenAI Gym environment (`SmartLoadEnv`) that simulates the backend pool:
    - **State**: `[latency_backend_i, queue_depth_backend_i, health_flag_backend_i]` for each backend
    - **Action**: discrete — select backend index to route the next request to
    - **Reward**: `−P95_latency − λ × load_imbalance_penalty`
    - **Episode**: one episode = a workload trace from `load_borg_timeseries()`
  - Save environment class to `services/rl-engine/env.py`

- [ ] **N2.4 — RL training: PPO offline**
  - Train PPO agent (Stable-Baselines3) in `SmartLoadEnv`
  - Monitor reward curves; train until convergence
  - Compare RL routing vs round-robin on held-out Borg traces → record P95 latency + throughput
  - Save final checkpoint: `services/rl-engine/models/policy.zip`

- [ ] **N2.5 — RL engine: active mode**
  - Load `policy.zip` on startup
  - Change `mode` in `RoutingRecommendation` from `"shadow"` to `"active"` (controlled by `operating_mode` from policy manager)
  - Tasneem's T2.1 sidecar will consume these recommendations and update NGINX weights

---

### Rghda

- [ ] **R2.1 — Alibaba and Yahoo SMD datasets**
  - Download, parse, and preprocess both datasets following the same pattern as R1.1 and R1.2
  - Add loaders to `datasets/loaders.py`:
    - `load_alibaba_traces()` → DataFrame for RL + Forecasting
    - `load_yahoo_smd(group: int)` → DataFrame with anomaly labels for Anomaly Detection

- [ ] **R2.2 — Model evaluation scripts**
  - Write `datasets/evaluate_anomaly.py`:
    - Load test split of NAB / Yahoo SMD
    - Run anomaly detector predictions
    - Report precision, recall, F1, false-positive rate
  - Write `datasets/evaluate_forecasting.py`:
    - Load held-out Borg / Alibaba traces
    - Run forecasting model predictions
    - Report MAE, RMSE, MAPE per horizon
  - These scripts produce the numbers that go in the final report

- [ ] **R2.3 — Dataset documentation**
  - Complete `datasets/<name>/README.md` for all 4 datasets
  - Document: preprocessing decisions, feature engineering choices, train/test split rationale, any data quality issues

---

## Phase 3 — Testing and Benchmarking

**Goal:** Quantitative evidence that SmartLoad with AI routing outperforms classical round-robin.

**Prerequisite:** Phase 2 complete.

**Deadline:** Mid June 2026

**All three work together.**

---

- [ ] **ALL.1 — Unit tests** (`tests/unit/`)
  - **Tasneem**: autoscaler scaling decisions (mock forecast → expected container count), policy manager API (GET/POST), routing hierarchy (anomaly exclusion overrides RL overrides classical)
  - **Nada**: anomaly detector (inject known-anomalous sequence → expect UNHEALTHY signal), forecasting (deterministic input → expected MAE), RL agent (fixed state → valid action in range)
  - **Rghda**: data loaders (correct column names, no NaN, correct shape), preprocessors (timestamp alignment, normalization range)

- [ ] **ALL.2 — Performance benchmarking**
  - **Scenario 1 — Steady load**: run Locust at constant RPS for 10 minutes; compare P95 latency and throughput with RL routing vs round-robin
  - **Scenario 2 — Burst load**: ramp Locust from 10 to 200 users in 30s; compare time-to-stabilize with forecasting-based autoscaling vs reactive autoscaling
  - **Scenario 3 — Degraded backend**: set one backend to `RESPONSE_DELAY_MS=2000`; measure time from first anomaly to traffic rerouted
  - Collect logs and metrics; export plots from Grafana

- [ ] **ALL.3 — Update `docs/running-the-project.md`**
  - Add final full-stack compose instructions (Phase 6 section)
  - Document how to reproduce each benchmark scenario

- [ ] **ALL.4 — Results for the final report**
  - Collect latency percentile tables (P50, P95, P99) per scenario
  - Collect throughput numbers
  - Export Grafana screenshots
  - Run R2.2 evaluation scripts and record anomaly detection and forecasting accuracy

---

## Dependency Graph

```
Phase 0 (parallel) ──────────────────────────────────────────────────────────────────────────────
  R0.1 → R0.2 → R0.3 → R0.4 ──────────────────────────────────────────────────────────────────→ Phase 1 (Rghda)
  N0.1 → N0.2 → N0.3 ─────────── (N0.3 reviews T0.2 before it merges) ─────────────────────────→ Phase 1 (Nada)
  T0.1 → T0.2* → T0.3 → T0.4 → T0.5 → T0.6 → T0.7 → T0.8 ──────────────────────────────────→ Phase 1 (Tasneem)
                  ↑
                  *T0.2 blocked until N0.3 confirms schema

Phase 1 (parallel, after T0.7) ──────────────────────────────────────────────────────────────────
  R1.1 → R1.2 → R1.3 → R1.4 ──────────────────────────────────────────────────────────────────→ Phase 2 (Rghda)
  N1.1 → N1.2 → N1.3 ─────────────────────────────────────────────────────────────────────────→ Phase 2 (Nada)
  T1.1 → T1.2 → T1.3 → T1.4 → T1.5 → T1.6 ──────────────────────────────────────────────────→ Phase 2 (Tasneem)
                                              ↑
                                              R1.4 (data loaders) required by Nada Phase 2

Phase 2 (parallel, after Phase 1 + datasets) ────────────────────────────────────────────────────
  R2.1 → R2.2 → R2.3 ─────────────────────────────────────────────────────────────────────────→ Phase 3
  N2.1 → N2.2 → N2.3 → N2.4 → N2.5 ──────────────────────────────────────────────────────────→ Phase 3
  T2.1 → T2.2 → T2.3 ─────────────────────────────────────────────────────────────────────────→ Phase 3

Phase 3 (all together) ──────────────────────────────────────────────────────────────────────────
  ALL.1 → ALL.2 → ALL.3 → ALL.4
```

**Critical path (must not block):**
1. `T0.6` — full docker-compose. Nothing can be tested in the real stack until this is done. **Tasneem's #1 priority.**
2. `N0.1` + `N0.2` — Redis contracts + DB queries. Tasneem's schema depends on these. **Nada's #1 priority.**
3. `R1.4` — data loaders. Nada's Phase 2 ML training depends on these. **Rghda's #1 priority in Phase 1.**
