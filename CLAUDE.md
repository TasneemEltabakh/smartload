# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SmartLoad is middleware (and candidate SaaS) for AI-driven load management: NGINX load balancing combined with a
"decision plane" of Python services (anomaly-detector, forecasting, rl-engine, autoscaler, policy-manager) that make
adaptive routing and autoscaling decisions while preserving deterministic safety fallbacks. Originally a graduation
project (Zewail City, CIE 2025/2026, Team 09).

**Canonical spec**: [`docs/SOURCE_OF_TRUTH.html`](docs/SOURCE_OF_TRUTH.html) — every architectural decision lives
here; the "Find what you need — by persona" panel routes to the right section. `docs/PROJECT_WALKTHROUGH.md` is the
narrative file-by-file tour. Read these before making non-trivial architectural changes.

## Commands

### Local stack (Docker Compose)
```bash
cp config/.env.example .env
docker compose up -d --build           # 14 containers
docker compose down
```

### Lint (CI hard gate)
```bash
ruff check services test-backends
```

### Structural / contract lints (anti-drift, currently permissive — see #139)
```bash
python scripts/lint-structure.py       # per-service README + plugin layout + e2e/feature alignment
python scripts/lint-redis-channels.py  # every Redis channel must appear in docs/redis-channels.md
python scripts/lint-openapi.py         # every /api/v1/* route must appear in docs/openapi/smartload-v1.yaml
```

### Tests
```bash
pytest tests/unit/                     # pure-function, no live stack
pytest tests/integration/              # needs docker compose up -d
pytest tests/e2e/                      # feature-level, via the SDK
pytest tests/conformance/              # one suite per plugin contract (e.g. lb_adapter)

# single test file / class / method
pytest tests/unit/anomaly-detector/test_runloop.py
pytest tests/unit/anomaly-detector/test_runloop.py::TestClassName::test_method
```
Each `tests/unit/<service>/` directory must be run as its own pytest invocation in CI — top-level modules
(`engine_base`, `runloop`, `engines`, `policies`) collide in `sys.modules` across services if collected together.
`tests/` are PEP 420 namespace packages (no `__init__.py`); `pytest.ini` sets `pythonpath = .` so
`from services.shared.contracts import ...` resolves.

### Per-service local run (example: anomaly-detector)
```bash
pip install -r services/anomaly-detector/requirements.txt
python services/anomaly-detector/app.py     # PORT env var
```

### Offline ML training (anomaly-detector IsolationForest)
```bash
python tools/anomaly-training/train_smd.py --smd-dir datasets/smd --mst-dir datasets/alibaba/mst2021/MSCallGraph/extracted_0
# Outputs: services/anomaly-detector/models/isolation_forest.pkl (bundle, see below),
#          tools/anomaly-training/training_log.json (appended, pipeline="smd")
```
Canonical pipeline — trains on SMD (Server Machine Dataset / OmniAnomaly) with real `test_label/` ground truth,
searching machine sets / SMD dim→feature mappings / rolling windows / contamination for the best F1 on a held-out
split (current result: `test_f1=0.8012` > 0.80 gate, see `engines/isolation_forest/README.md`). The saved `.pkl` is
a **bundle dict**, not a bare model: `{model, smd_scaler, production_scaler, feature_order, thresholds, metadata}`.
`production_scaler` (fit on MST-2021 features) reconciles SMD's per-machine [0,1] normalization with production's
real-ms `ANOMALY_QUERY` features at inference time. `tools/anomaly-training/train.py` (MST-2021 traces, invented
"population-relative" labels, `test_f1=0.10`) is retained only as a superseded historical record
(`training_log.json` entry with `pipeline="mst"`).

## Architecture

### Request path
```
Client → load-balancer (NGINX, :8080) → test-backend pool (5 replicas)
```
`lb-sidecar` (:8087) subscribes to `smartload.routing` / `smartload.anomaly` / `smartload.policy`, rewrites
`upstream.conf`, and reloads NGINX via `docker exec`. It discovers the live backend set via Docker label query on
every Redis message, so backends provisioned dynamically by the autoscaler appear automatically.

### Engine-wrapper pattern (the core abstraction — issue #138)
`anomaly-detector`, `forecasting`, and `rl-engine` all share the same shape:
- `app.py` — Flask app + background thread running the poll loop
- `runloop.py` — pure-Python, unit-testable: drains `smartload.policy` (rebuild engine on update), queries
  TimescaleDB via `services/shared/queries.py`, runs the engine, publishes an envelope
- `engine_base.py` (or `policy_base.py`) — ABC + dataclasses (e.g. `BackendFeatures`, `AnomalyScore`) +
  `select_engine(name)` / `select_policy(name)` factory with automatic fallback to a baseline if the named
  engine/model can't load
- `engines/<name>/` (or `policies/<name>/`) — one self-contained folder per implementation: `engine.py`,
  `README.md`, `test_engine.py`. Never add implementations as flat files outside this layout.

**Adding a new engine/model**: drop `services/<svc>/models/<name>.pkl` (or `policy.zip` for RL), implement
`engines/<name>/engine.py` subclassing the ABC, register the name in the `select_engine`/`select_policy` factory,
set `<SVC>_ENGINE` (or `RL_POLICY`) env var. No service-shell (`app.py`/`runloop.py`) changes needed. If the
artifact is missing or fails to load, the factory falls back to the baseline automatically.

Each service is gated by `<SVC>_RUNLOOP_ENABLED` (default `true`). `rl-engine` additionally has `RL_MODE`
(`shadow` | `active`) — `shadow` is the safety pin; the lb-sidecar ignores RL envelopes unless `mode == "active"`.

### anomaly-detector specifics
- `engine_base.BackendFeatures`: `backend_id, latency_ms, latency_rolling_mean_ms, error_rate, sample_count,
  latency_rolling_std_ms`. Feature order for ML engines is defined by `FEATURE_ORDER` in
  `engines/isolation_forest/engine.py` and MUST match `tools/anomaly-training/train_smd.py`'s `FEATURE_COLUMNS`
  (and the bundle's `feature_order` field, validated at load time).
- `ANOMALY_ENGINE`: `threshold` (rule-based, default) | `isolation_forest` (trained sklearn model, F1=0.8012 > 0.80
  gate — see `engines/isolation_forest/README.md`).
- Training pipeline (`tools/anomaly-training/train_smd.py`) trains on SMD (`datasets/smd/`, per-machine
  `train/`/`test/`/`test_label/`/`interpretation_label/`) with real anomaly labels, searching SMD dim→feature
  mappings, rolling windows, and contamination across machine sets; reports F1/precision/recall on a held-out split
  never used for tuning. `preprocess_mst.py`/MST-2021 traces (`datasets/alibaba/mst2021/MSCallGraph/`) are still
  used to fit the bundled `production_scaler` (domain adaptation to real-ms production features) but no longer for
  ground truth. The earlier MST-only pipeline (`train.py`, invented "population-relative" labels, `test_f1=0.10`)
  is superseded — see `training_log.json` (`pipeline` field distinguishes runs).

### Shared module (`services/shared/`)
- `contracts.py` — Redis pub/sub envelope dataclasses (every envelope carries a `version` field; subscribers must
  tolerate unknown fields)
- `queries.py` — canonical SQL constants against TimescaleDB
- `lb_adapters/` — plugin-per-folder adapters (nginx / envoy / haproxy / alb); each must pass
  `tests/conformance/lb_adapter/`

Docker builds for services that depend on `shared/` use `context: ./services` (not the service subfolder) so the
Dockerfile can `COPY` `shared/` alongside the service code — see `docker-compose.yml`.

### Contracts (single source of truth per surface)
| Surface | File |
|---|---|
| HTTP REST | `docs/openapi/smartload-v1.yaml` (OpenAPI 3.1) |
| Redis pub/sub | `docs/redis-channels.md` |
| DB schema | `infrastructure/timescaledb/init.sql` |
| Python SDK | `clients/python/smartload_client/` |
| Per-feature manifests | `docs/features/<feature>.md` |

New features: add `tests/e2e/<feature>/`, `docs/features/<feature>.md`, and `examples/scenarios/<feature>/` together
— `scripts/lint-structure.py` checks this triangulation.

### Operator UI vs demo-ui
- `services/operator-ui` (:8090) — production transparency + override layer (Home, Policy, Audit, Actions pages).
  Flask BFF + React web. Per the SOT lock, this is NOT an admin panel; tenants integrate via SDK/webhooks.
- `tools/demo-ui` (:8091) — developer-only end-to-end validation harness: scenario injection, chaos, live SSE feed,
  benchmark results viewer. Not shipped middleware.

### Experiments
`experiments/<feature>_<UTC-timestamp>/` — frozen one-off integration/smoke/benchmark run artifacts, readable via
git log. `experiments/baseline-vs-smartload/results/` is mounted read-only into demo-ui's Benchmark page.
