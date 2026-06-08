# SmartLoad

**SmartLoad is middleware (and a candidate SaaS) for AI-driven load management.** It sits between client traffic and a pool of backend services and combines classical load balancing with telemetry-driven decision intelligence — anomaly detection, workload forecasting, and reinforcement-learning routing — to make adaptive routing and proactive autoscaling decisions while preserving deterministic safety fallbacks. Customers run it on-prem (Docker Compose / Helm) or, in the future, consume it via the managed control plane.

```
                 ┌────────────────────────────────────────────────┐
   Client ──►   │  load-balancer (NGINX)                          │  ──►  backend pool
                 │   ▲ rewrite upstream weights                    │
                 │   │                                             │
                 │   └── decision plane:                           │
                 │        anomaly-detector · forecasting           │
                 │        rl-engine · autoscaler · policy-manager  │
                 │   └── operator-ui (BFF + web)                   │
                 │   └── webhook-dispatcher  ─────────────────────► customer URL
                 └────────────────────────────────────────────────┘
```

> **Looking for the canonical spec?** Every architectural decision lives in [`docs/SOURCE_OF_TRUTH.html`](docs/SOURCE_OF_TRUTH.html). The "Find what you need — by persona" panel at the top of the SOT routes you to the right section in under 10 seconds.

## Documentation map — where to go

Pick the section that matches what you're trying to do. Every link points into the canonical SOT (`docs/SOURCE_OF_TRUTH.html`).

| You want to… | Go to |
|---|---|
| **Call the API** from a script or external system | [§26 API Integration Guide](docs/SOURCE_OF_TRUTH.html#sec-26-api-guide) — every endpoint, error model, four integration patterns, curl walkthrough |
| **Use the Python SDK** | [§27 SDK Reference](docs/SOURCE_OF_TRUTH.html#sec-27-sdk) — every method, examples, threading model |
| **Use the operator UI** or understand its architecture | [§28 Operator UI Guide](docs/SOURCE_OF_TRUTH.html#sec-28-operator-ui) — pages, workflows, BFF endpoints, security |
| **Receive webhooks** from SmartLoad | [§29 Webhooks Specification](docs/SOURCE_OF_TRUTH.html#sec-29-webhooks) — registration, HMAC signing, retry, verification |
| **Design or query the database** | [§30 Database Design Consolidation](docs/SOURCE_OF_TRUTH.html#sec-30-database) — every table, index, retention, query catalog |
| **Understand the architecture** | [§5 Big Picture](docs/SOURCE_OF_TRUTH.html#sec-4-architecture) + [§16 Plane Split](docs/SOURCE_OF_TRUTH.html#sec-control-plane) |
| **Self-host or deploy** | [§25 Distribution](docs/SOURCE_OF_TRUTH.html#sec-25-distribution) + [§20 Deployment](docs/SOURCE_OF_TRUTH.html#sec-14-deploy) |
| **Contribute to a service** | [§7 Service Directory](docs/SOURCE_OF_TRUTH.html#sec-5-directory) + [§8 Deep Dives](docs/SOURCE_OF_TRUTH.html#sec-6-deepdives) |
| **Read for thesis / research** | [§2 Overview](docs/SOURCE_OF_TRUTH.html#sec-2-overview) → [§14 ML Foundations](docs/SOURCE_OF_TRUTH.html#sec-9-data) → [§15 Routing Authority](docs/SOURCE_OF_TRUTH.html#sec-10-routing) → [§22 Changelog](docs/SOURCE_OF_TRUTH.html#sec-15-changelog) → **[§31 Related Work](docs/SOURCE_OF_TRUTH.html#sec-31-related-work) · [§32 Algorithm Foundations](docs/SOURCE_OF_TRUTH.html#sec-32-algorithms) · [§33 Methodology](docs/SOURCE_OF_TRUTH.html#sec-33-methodology) · [§34 Results & Discussion](docs/SOURCE_OF_TRUTH.html#sec-34-results) · [§35 Limitations & Future Work](docs/SOURCE_OF_TRUTH.html#sec-35-limitations)** |

---

## What it does

- **Anomaly-aware routing.** A backend that starts misbehaving is excluded from the upstream pool before clients see the latency hit.
- **Forecast-driven autoscaling.** The pool grows ahead of the spike, not in response to it.
- **Reinforcement-learning routing.** Trained against real workload traces; switchable between `shadow` (observe-only) and `active` per policy.
- **Operator-first overrides.** A `safe_mode` flag forces every engine to the deterministic fallback. Every change is audit-logged.

---

## Writing about SmartLoad (thesis / poster / presentation)

The three docs are designed to be the only source you need for a thesis, poster, or presentation — no repo reading required.

| Artefact | Lift from |
|---|---|
| **Thesis — Introduction** | [SOT §2 Executive Overview](docs/SOURCE_OF_TRUTH.html#sec-2-overview) + [§3 Canonical Project Definition](docs/SOURCE_OF_TRUTH.html#sec-3-definition) |
| **Thesis — Background / Related Work** | [SOT §31 Background & Related Work](docs/SOURCE_OF_TRUTH.html#sec-31-related-work) (inline citations are lift-ready) |
| **Thesis — System Design** | [SOT §4 Principles](docs/SOURCE_OF_TRUTH.html#sec-design-principles) + [§5 Big Picture](docs/SOURCE_OF_TRUTH.html#sec-4-architecture) + [§8 Service Deep Dives](docs/SOURCE_OF_TRUTH.html#sec-6-deepdives) + [§12 Diagrams](docs/SOURCE_OF_TRUTH.html#sec-architecture-map) + [§15 Routing Authority](docs/SOURCE_OF_TRUTH.html#sec-10-routing) |
| **Thesis — Algorithms / Methodology** | [SOT §32 Algorithm Foundations](docs/SOURCE_OF_TRUTH.html#sec-32-algorithms) + [§33 Evaluation Methodology](docs/SOURCE_OF_TRUTH.html#sec-33-methodology) + [Walkthrough §8 Algorithms & training procedure](docs/PROJECT_WALKTHROUGH.md#8-algorithms--training-procedure) |
| **Thesis — Implementation** | [Walkthrough](docs/PROJECT_WALKTHROUGH.md) (file-by-file tour, code excerpts, §1–§8) |
| **Thesis — Results / Discussion** | [SOT §34 Results & Discussion](docs/SOURCE_OF_TRUTH.html#sec-34-results) — synthesised honest finding; raw run-by-run numbers in [§22 Changelog](docs/SOURCE_OF_TRUTH.html#sec-15-changelog) v1.0.7r/s/t |
| **Thesis — Limitations / Future Work** | [SOT §35 Limitations & Future Work](docs/SOURCE_OF_TRUTH.html#sec-35-limitations) |
| **Thesis — Conclusion** | [SOT §31.7 Positioning in one paragraph](docs/SOURCE_OF_TRUTH.html#sec-31-related-work) + [§34.5 What this confirms](docs/SOURCE_OF_TRUTH.html#sec-34-results) |
| **Poster — Problem statement** | [SOT §2 Problem statement](docs/SOURCE_OF_TRUTH.html#sec-2-overview) (one paragraph, lift-ready) |
| **Poster — System diagram** | [SOT §5 Figure 5.1 Context](docs/SOURCE_OF_TRUTH.html#sec-4-architecture) + [Figure 5.2 Layer](docs/SOURCE_OF_TRUTH.html#sec-4-architecture) + [Figure 5.3 MAPE Loop](docs/SOURCE_OF_TRUTH.html#sec-4-architecture) (Mermaid sources) |
| **Poster — Contribution** | [SOT §31.7 Positioning paragraph](docs/SOURCE_OF_TRUTH.html#sec-31-related-work) |
| **Poster — Headline numbers** | [SOT §34.3 Per-phase p95 table](docs/SOURCE_OF_TRUTH.html#sec-34-results) (honest, including the +3 s max-latency cost) |
| **Presentation — Story arc** | §2 (what / why) → §31 (where the field is) → §5 + §15 (the architecture) → §32 (how the engines work) → §33 (how we evaluate) → §34 (what we found) → §35 (what's next) |
| **Presentation — Demo flow** | [SOT §28 Operator UI Guide](docs/SOURCE_OF_TRUTH.html#sec-28-operator-ui) (policy → audit → manual actions → status) + the demo-ui benchmark page from [Walkthrough §5.5](docs/PROJECT_WALKTHROUGH.md#55-toolsdemo-ui--developer-demo-harness) |
| **Presentation — Honest read** | [SOT §34.3–§34.6](docs/SOURCE_OF_TRUTH.html#sec-34-results) — the harness works, the mechanism works, the trained policy needs retraining on heterogeneous traces; the binding constraint is named. |

> Every cross-section reference inside the SOT is a working hash anchor — Ctrl-F by section number to jump.

---

## Quick Start (Docker Compose)

```bash
git clone <repo-url> && cd smartload
cp config/.env.example .env             # fill in values
docker compose up -d                    # 14 containers come up
```

What you get:

| Surface | URL | Notes |
|---|---|---|
| Client traffic ingress | `http://localhost:8080` | NGINX load balancer |
| Operator UI | `http://localhost:8090` | Home page + Policy editor + API docs |
| API docs (Swagger UI) | `http://localhost:8090/api/docs` | Live from `docs/openapi/smartload-v1.yaml` |
| Locust traffic simulator | `http://localhost:8089` | Run synthetic load |
| Grafana | `http://localhost:3000` | Telemetry dashboards (admin / admin) |
| Prometheus | `http://localhost:9090` | Metrics |

---

## Try it from Python (SDK)

```bash
pip install -e clients/python                                # editable install
python clients/python/examples/quickstart.py                 # prints current policy
python examples/scenarios/policy-management/policy_walk.py   # end-to-end walkthrough
```

```python
from smartload_client import SmartLoadClient

with SmartLoadClient(base_url="http://localhost:8086") as c:
    policy = c.get_policy()
    print(policy["operating_mode"], policy["safe_mode"])
    c.set_policy({"safe_mode": True}, actor="my-tool")
    c.subscribe_policy(lambda payload, meta: print("policy changed:", payload))
```

For full SDK reference (every method, exception type, threading model) see [SOT §27](docs/SOURCE_OF_TRUTH.html#sec-27-sdk). For working examples: [`clients/python/examples/`](clients/python/examples/).

---

## Integrate as external middleware

SmartLoad publishes events over **two channels** so any integrator can consume them:

| Channel | When to use | Reference |
|---|---|---|
| **Redis pub/sub** (sub-second latency, in-network) | You can run a Redis client, you need decisions within seconds. | SDK `subscribe_policy()` — [SOT §27.4](docs/SOURCE_OF_TRUTH.html#sec-27-sdk) |
| **Webhooks** (HMAC-signed HTTP POSTs, ~30s latency, public-internet friendly) | You speak HTTP, you can host a public endpoint, you want at-least-once delivery with retries. | [SOT §29 Webhooks](docs/SOURCE_OF_TRUTH.html#sec-29-webhooks) — registration, HMAC, retry, customer verification (Python + Node) |

The full integration patterns matrix (read-only console, synchronous operator, Redis listener, webhook consumer) is in [SOT §26.9](docs/SOURCE_OF_TRUTH.html#sec-26-api-guide).

---

## Operator UI

The web UI at `http://localhost:8090` is the operator-facing transparency + override surface. Four pages shipped today:

- **Home** — service-health grid for every SmartLoad service, polled every 10 s (slice #1)
- **Policy** — read current policy · edit JSON · side-by-side diff preview · commit with audit trail (slice #1)
- **Audit** — unified view over both audit streams (policy_changes + scaling_events) with kind / actor / action / limit filters, auto-refresh, colour-coded action badges (slice #2)
- **Actions** — operator overrides: scale to N backends, isolate a backend, each with a confirmation modal that previews the state change; results feed of the last 10 actions; every action lands on the Audit page with a `manual:<actor>:` prefix (slice #3)

For workflow walkthroughs, BFF endpoint reference, configuration, security posture, and the roadmap to OUI.3 through OUI.8 see [SOT §28 Operator UI Guide](docs/SOURCE_OF_TRUTH.html#sec-28-operator-ui).

Per the SOT lock (commit `6f89a13`), the operator UI is a **transparency + override layer**, not an admin panel — tenants integrate via the SDK / webhooks, not the UI.

---

## Contracts (single source of truth per surface)

| Surface | Canonical contract | Reference |
|---|---|---|
| HTTP REST | [`docs/openapi/smartload-v1.yaml`](docs/openapi/smartload-v1.yaml) (OpenAPI 3.1) | [SOT §26 API Guide](docs/SOURCE_OF_TRUTH.html#sec-26-api-guide) (prose) |
| Redis pub/sub | [`docs/redis-channels.md`](docs/redis-channels.md) | [SOT §11](docs/SOURCE_OF_TRUTH.html#sec-interface-authority) (envelope rules) |
| Webhooks | inline in OpenAPI spec (planned #130) | [SOT §29 Webhooks](docs/SOURCE_OF_TRUTH.html#sec-29-webhooks) |
| Python SDK | `clients/python/smartload_client/` | [SOT §27 SDK Reference](docs/SOURCE_OF_TRUTH.html#sec-27-sdk) |
| Database schema | [`infrastructure/timescaledb/init.sql`](infrastructure/timescaledb/init.sql) | [SOT §30 DB Design](docs/SOURCE_OF_TRUTH.html#sec-30-database) (consolidated) |
| Per-feature manifests | [`docs/features/`](docs/features/) | one file per shipped feature |
| Architecture (in-tree) | [`docs/architecture/`](docs/architecture/) | control / data plane, multi-tenancy, failure modes |
| Whole-system | [`docs/SOURCE_OF_TRUTH.html`](docs/SOURCE_OF_TRUTH.html) | single navigable reference for everything else |

Three CI guardrails enforce the contracts:
- `scripts/lint-redis-channels.py` — every Redis channel in source must appear in the registry
- `scripts/lint-openapi.py` — every `/api/v1/*` route in source must appear in the OpenAPI spec
- `scripts/lint-structure.py` — every `tests/e2e/<feature>/` must have a sibling `docs/features/<feature>.md` + `examples/scenarios/<feature>/`

---

## Repository structure

This repo follows a **role-based** organisation: services mirror deployment topology, plugins live one folder per implementation, and feature artefacts triangulate across `docs/features/` + `examples/scenarios/` + `tests/e2e/`.

```
smartload/
├── services/                         # one folder per deployable service
│   ├── load-balancer/                # NGINX
│   ├── lb-otel-shipper/              # NGINX log → OTLP shipper
│   ├── lb-sidecar/                   # dynamic upstream rewriter (T2.1)
│   ├── telemetry/                    # OTLP ingress + read API
│   ├── anomaly-detector/             # plugin-per-folder under engines/
│   ├── forecasting/                  # plugin-per-folder under engines/
│   ├── rl-engine/                    # plugin-per-folder under policies/
│   ├── autoscaler/                   # Docker SDK → test-backend pool
│   ├── policy-manager/               # operating policy + audit
│   ├── operator-ui/                  # bff/ (Flask) + web/ (React)
│   └── shared/
│       ├── contracts.py              # Redis envelope dataclasses
│       ├── queries.py                # canonical SQL constants
│       └── lb_adapters/              # nginx / envoy / haproxy / alb (plugin-per-folder)
├── clients/python/                   # smartload_client SDK + examples + tests
├── examples/
│   ├── scenarios/<feature>/          # runnable end-to-end demos
│   └── deployments/                  # reference deployment shapes
├── infrastructure/
│   ├── grafana/ otel-collector/ prometheus/ redis/ timescaledb/
│   ├── k8s/                          # raw manifests
│   └── helm/smartload/               # Chart.yaml + values.yaml (#133)
├── tests/                            # PEP 420 namespace packages — no __init__.py
│   ├── unit/<service>/               # pure-function tests per service
│   ├── integration/                  # service-pair / contract tests
│   ├── e2e/<feature>/                # feature-level tests via the SDK
│   ├── conformance/lb_adapter/       # every adapter must pass these
│   └── performance/                  # Locust
├── tools/                            # dev-utility containers (NOT shipped middleware)
│   ├── demo-ui/                      # scenario-injection + chaos + live SSE feed (8091)
│   └── traffic-simulator/            # Locust UI for synthetic load (8089)
├── experiments/                      # one-off integration / smoke run artifacts
│   └── <feature>_<UTC-timestamp>/    # frozen by date; readable via git log
├── docs/
│   ├── SOURCE_OF_TRUTH.html          # canonical spec (read first)
│   ├── PROJECT_WALKTHROUGH.md        # narrative walkthrough
│   ├── features/                     # per-feature manifests (+ SLICE_CHECKLIST.md)
│   ├── architecture/                 # control/data plane, multi-tenancy, failure modes
│   ├── openapi/                      # smartload-v1.yaml
│   ├── planned/                      # placeholder docs for unimplemented services (e.g. webhook-dispatcher)
│   ├── ui-mockups/                   # operator-UI page mockups (PNG)
│   └── redis-channels.md             # canonical channel registry
├── scripts/
│   ├── lint-structure.py             # per-service README + plugin layout + e2e/feature alignment
│   ├── lint-redis-channels.py        # channel-registry anti-drift
│   ├── lint-openapi.py               # spec anti-drift
│   ├── seed-metrics.py               # synthetic telemetry seeder
│   └── download-datasets.sh
├── config/                           # policy.yaml + .env.example
├── datasets/                         # public training data (Borg, Alibaba, NAB, Yahoo SMD); fetched, gitignored
├── test-backends/                    # Node.js stubs the autoscaler scales
└── docker-compose.yml
```

A complete canonical tree with placement rules is in [SOT §7](docs/SOURCE_OF_TRUTH.html).

---

## Services

| Service | Language | Port | Status |
|---|---|---|---|
| `load-balancer` | NGINX | 8080 | wired |
| `lb-otel-shipper` | Python | sidecar | T1.2 shipped |
| `lb-sidecar` | Python | 8087 | T2.1 shipped — subscribes to `smartload.routing` + `smartload.anomaly` + `smartload.policy`, dynamically rewrites `upstream.conf`, `nginx -s reload` via Docker exec |
| `telemetry` | Python | 8081 | T1.1 shipped (OTLP ingest + read API) |
| `anomaly-detector` | Python | 8082 | Phase-1 run loop wired (#138 round 1, `ANOMALY_RUNLOOP_ENABLED=true` default since v1.0.7g) + `/api/v1/isolate` (slice #3, #123) |
| `forecasting` | Python | 8083 | Phase-1 run loop wired (#138 round 2, `FORECAST_RUNLOOP_ENABLED=true` default since v1.0.7g) |
| `rl-engine` | Python | 8084 | Phase-1 run loop wired (#138 round 3, `RL_RUNLOOP_ENABLED=true` default since v1.0.7g; `RL_MODE=shadow` is the safety pin that keeps routing inert until an operator opts in); v1.0.7 review fixes (see SOT §22) |
| `autoscaler` | Python | 8085 | T1.3 shipped + `/api/v1/audit/scaling` (slice #2) + `/api/v1/scale` (slice #3, #123) |
| `policy-manager` | Python | 8086 | T1.4 shipped + `/api/v1/audit/policy` |
| `operator-ui` | Flask + React | 8090 | Home + Policy + Audit + Actions pages (slices #1, #2, #3) |
| `webhook-dispatcher` | — | — | placeholder; tracking doc lives at `docs/planned/webhook-dispatcher.md` until #130 lands the implementation |

Infrastructure: TimescaleDB · Redis · OTel Collector · Prometheus · Grafana — all configured under `infrastructure/`.

### Engine-wrapper foundation (#138 — cutover complete)

All three AI services (`anomaly-detector`, `forecasting`, `rl-engine`) share an identical run-loop shape: load an engine/policy via `select_engine()` / `select_policy()` with automatic fallback to a baseline, then per tick — drain `smartload.policy` (rebuild the engine on update), query TimescaleDB, run the engine, and publish an envelope. The pattern is split between `app.py` (Flask + thread) and `runloop.py` (pure-Python unit-testable pieces). Each service is enabled by default since v1.0.7g (`<SVC>_RUNLOOP_ENABLED=true` in `docker-compose.yml`) now that the smoke runs have shipped; an operator can still pin a service back to the Phase-0 stub by setting the flag to `false` in `.env`. The remaining safety is the `RL_MODE=shadow` pin on the rl-engine and the LB sidecar's `mode != "active"` gate — both still default safe, so RL publishes shadow envelopes the sidecar ignores until an operator opts in.

- **anomaly-detector** — `ANOMALY_RUNLOOP_ENABLED` + `ANOMALY_ENGINE` (threshold | isolation_forest)
- **forecasting** — `FORECAST_RUNLOOP_ENABLED` + `FORECAST_ENGINE` (moving_average | arima)
- **rl-engine** — `RL_RUNLOOP_ENABLED` + `RL_POLICY` (random_shadow | ppo) + `RL_MODE` (shadow | active operator pin)

Total 63 unit tests across the three services (18 + 19 + 26) cover bootstrap fallback, policy parsing, row pivot, publish gate, mode composition, health classification.

Diagrams (engine bootstrap, run-loop cycle, RL mode composition, cutover progress): [SOT §25.6](docs/SOURCE_OF_TRUTH.html#sec-25-distribution) and [PROJECT_WALKTHROUGH §4](docs/PROJECT_WALKTHROUGH.md#4-decision-plane).

**Model handoff is now stable for all three:** drop `services/<svc>/models/<name>.pkl` (or `policy.zip` for RL), implement `engines/<name>/engine.py` (or `policies/<name>/policy.py`) subclassing the service ABC, register the name in the factory, and set `<SVC>_ENGINE=<name>` (or `RL_POLICY`). No service-shell changes. Falls back to baseline automatically if the artifact is missing.

---

## Examples & scenarios

Runnable Python scripts that exercise each shipped feature end-to-end. New features must add a script here before they are considered "done."

| Feature | Scenario script | Status |
|---|---|---|
| Policy management | [`examples/scenarios/policy-management/policy_walk.py`](examples/scenarios/policy-management/policy_walk.py) | shipped |
| Audit log viewer | [`examples/scenarios/audit-log/audit_walk.py`](examples/scenarios/audit-log/audit_walk.py) | shipped |
| Manual actions | [`examples/scenarios/manual-actions/manual_actions_walk.py`](examples/scenarios/manual-actions/manual_actions_walk.py) | shipped |
| Forecast burst → scale-out | `examples/scenarios/forecast-autoscale/` | planned |
| Anomaly → reroute | `examples/scenarios/anomaly-routing/` | planned (depends on T2.1) |

Reference deployments live under [`examples/deployments/`](examples/deployments/).

---

## Testing

```bash
# Pure-function tests (no live stack required)
pytest tests/unit/

# Service-pair / wire-protocol tests (need docker compose up -d)
pytest tests/integration/

# Feature-level end-to-end tests via the SDK
pytest tests/e2e/

# Interface conformance suites (one per plugin contract)
pytest tests/conformance/

# Load tests
docker compose up traffic-simulator
# open http://localhost:8089
```

CI structural lints (permissive today; strict mode tracked by #139):

```bash
python scripts/lint-structure.py
python scripts/lint-redis-channels.py
python scripts/lint-openapi.py
```

---

## Datasets

Public datasets used by the ML services. `scripts/download-datasets.sh` fetches them.

| Dataset | Used by | License |
|---|---|---|
| Google Borg Cluster Traces | rl-engine, forecasting | CC-BY |
| Alibaba Microservice Traces | rl-engine, forecasting, anomaly-detector | Open |
| Numenta Anomaly Benchmark (NAB) | anomaly-detector | MIT |
| Yahoo Server Machine Dataset (SMD) | anomaly-detector | Open |

---

## Configuration

- `config/policy.yaml` — operating policy (mode, safe_mode, SLOs, scaling limits). Edited via the operator UI or `POST /api/v1/policy`.
- `config/.env.example` — template for required env vars (DB password, Redis URL, model paths). Copy to `.env` at the repo root.
- Per-service runtime knobs read from env vars; see each service's `README.md`.

API versioning: `/api/v1` is the current stable surface. Breaking changes follow path versioning + a `Sunset`/`Deprecation` header window (tracked by issue #134).

---

## CI/CD

Pipeline defined in `.github/workflows/`. Triggers on push and PR to `main`.

| Job | What it does |
|---|---|
| `lint` | `ruff check` on all Python; three structural lints (`scripts/lint-*.py`) in permissive mode |
| `unit-tests` | Pure-Python tests including the SDK suite |
| `build-services` | Matrix build for every service; health-check each container |
| `compose-test` | Spin up the full stack and run integration + e2e suites against it |

The structural lints will flip from warnings to fail-on-violation once issue #139 closes.

---

## Stability and Versioning

- **HTTP API**: path versioning (`/api/v1`, `/api/v2`). Breaking changes require a successor path + at least one quarter of overlap.
- **`policy.yaml` schema**: `schema_version` field is the migration anchor (planned via #134).
- **Redis envelopes**: every envelope carries a `version` field; subscribers must tolerate unknown fields.
- **SDK**: semver discipline; major bumps mirror API breaking changes.

---

## Project provenance

Originally developed as a graduation project at Zewail City of Science, Technology, and Innovation (CIE 2025/2026, Team 09: Tasneem Muhammed, Nada Nabil, Rghda Salah; supervisors Dr. Tamer Ashour, Dr. Doaa Shawky). The canonical design history is in [`docs/SOURCE_OF_TRUTH.html`](docs/SOURCE_OF_TRUTH.html).

---

## License

See [`LICENSE`](LICENSE).
