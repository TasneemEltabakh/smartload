# SmartLoad

> Intelligent middleware that puts a thinking layer between your clients and your backend pool: anomaly-aware routing, forecast-driven autoscaling, and reinforcement-learning load balancing — with deterministic safety fallbacks.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker images](https://github.com/TasneemEltabakh/smartload/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/TasneemEltabakh/smartload/actions/workflows/docker-publish.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Status: beta](https://img.shields.io/badge/status-beta-orange.svg)](#roadmap)

SmartLoad sits between client traffic and a pool of backend services. It combines a classical load balancer (NGINX today; HAProxy / Envoy / ALB plugins planned) with a decision plane that detects anomalies, forecasts demand, and learns routing policy from real traffic — all behind a `safe_mode` switch that pins every engine to its deterministic fallback when you need it to.

```
                 ┌────────────────────────────────────────────────┐
   Client ──►   │  load-balancer (NGINX)                          │  ──►  backend pool
                 │   ▲ rewrite upstream weights                    │
                 │   │                                             │
                 │   └── decision plane:                           │
                 │        anomaly-detector · forecasting           │
                 │        rl-engine · autoscaler · policy-manager  │
                 │   └── operator-ui (BFF + web)                   │
                 │   └── webhook-dispatcher  ─────────────────────► your URL
                 └────────────────────────────────────────────────┘
```

## Why SmartLoad

- **Anomaly-aware routing.** A backend that starts misbehaving is excluded from the upstream pool before clients see the latency hit.
- **Forecast-driven autoscaling.** The pool grows ahead of the spike, not in response to it.
- **Reinforcement-learning routing.** Trained against real workload traces; switchable between `shadow` (observe-only) and `active` per policy.
- **Operator-first overrides.** A `safe_mode` flag forces every engine to the deterministic fallback. Every change is audit-logged.
- **Integrate with anything.** Redis pub/sub for sub-second decisions, HMAC-signed webhooks for cross-network HTTP delivery, Python SDK for everything else.
- **Self-hostable.** Docker Compose today, Helm chart in progress. No vendor lock-in.

---

## Quick start

```bash
git clone https://github.com/TasneemEltabakh/smartload.git
cd smartload
cp config/.env.example .env             # fill in values
docker compose up -d                    # 14 containers come up
```

| Surface | URL | Notes |
|---|---|---|
| Client traffic ingress | `http://localhost:8080` | NGINX load balancer |
| Operator UI | `http://localhost:8090` | Home + Policy editor + Audit + Actions |
| API docs (Swagger UI) | `http://localhost:8090/api/docs` | Live from `docs/openapi/smartload-v1.yaml` |
| Locust traffic simulator | `http://localhost:8089` | Synthetic load |
| Grafana | `http://localhost:3000` | Telemetry dashboards (`admin` / `admin`) |
| Prometheus | `http://localhost:9090` | Metrics |

---

## Use it from Python

```bash
pip install -e clients/python
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

Full SDK reference (every method, exception type, threading model): [SOT §27](docs/SOURCE_OF_TRUTH.html#sec-27-sdk). Working examples: [`clients/python/examples/`](clients/python/examples/).

---

## Integrate with anything

SmartLoad publishes decisions over two channels — pick whichever fits your stack:

| Channel | When to use | Reference |
|---|---|---|
| **Redis pub/sub** (sub-second latency, in-network) | You can run a Redis client and need decisions within seconds. | SDK `subscribe_policy()` — [SOT §27.4](docs/SOURCE_OF_TRUTH.html#sec-27-sdk) |
| **Webhooks** (HMAC-signed HTTP POSTs, ~30s latency, public-internet friendly) | You speak HTTP, can host a public endpoint, want at-least-once delivery with retries. | [SOT §29 Webhooks](docs/SOURCE_OF_TRUTH.html#sec-29-webhooks) |

Integration patterns matrix (read-only console, synchronous operator, Redis listener, webhook consumer): [SOT §26.9](docs/SOURCE_OF_TRUTH.html#sec-26-api-guide).

---

## Operator UI

Web UI at `http://localhost:8090`:

- **Home** — service-health grid for every SmartLoad service, polled every 10 s
- **Policy** — read / edit operating policy with diff preview and audit trail
- **Audit** — unified view over policy changes + scaling events with kind / actor / action filters
- **Actions** — operator overrides (scale to N backends, isolate a backend) with confirmation modals
- **Live Engines** — SSE stream of decision-plane envelopes (anomaly / forecast / routing / scale)

The operator UI is a **transparency + override surface**, not an admin panel — programmatic integrators use the SDK / webhooks, not the UI. Full guide: [SOT §28](docs/SOURCE_OF_TRUTH.html#sec-28-operator-ui).

---

## Architecture

Every architectural decision lives in [`docs/SOURCE_OF_TRUTH.html`](docs/SOURCE_OF_TRUTH.html) — the canonical spec, with hash anchors for every section.

| You want to… | Go to |
|---|---|
| Understand the system at a glance | [§5 Big Picture](docs/SOURCE_OF_TRUTH.html#sec-4-architecture) + [§16 Plane Split](docs/SOURCE_OF_TRUTH.html#sec-control-plane) |
| Call the API | [§26 API Guide](docs/SOURCE_OF_TRUTH.html#sec-26-api-guide) |
| Use the SDK | [§27 SDK Reference](docs/SOURCE_OF_TRUTH.html#sec-27-sdk) |
| Receive webhooks | [§29 Webhooks](docs/SOURCE_OF_TRUTH.html#sec-29-webhooks) |
| Self-host or deploy | [§25 Distribution](docs/SOURCE_OF_TRUTH.html#sec-25-distribution) + [§20 Deployment](docs/SOURCE_OF_TRUTH.html#sec-14-deploy) |
| Query the database | [§30 Database Design](docs/SOURCE_OF_TRUTH.html#sec-30-database) |
| Write a plugin / contribute to a service | [§7 Service Directory](docs/SOURCE_OF_TRUTH.html#sec-5-directory) + [§8 Deep Dives](docs/SOURCE_OF_TRUTH.html#sec-6-deepdives) + [`docs/PROJECT_WALKTHROUGH.md`](docs/PROJECT_WALKTHROUGH.md) |

---

## Services

| Service | Language | Port | Role |
|---|---|---|---|
| `load-balancer` | NGINX | 8080 | Client traffic ingress; reload-on-write of `upstream.conf` |
| `lb-otel-shipper` | Python | sidecar | Tails NGINX log, ships OTLP/HTTP-JSON |
| `lb-sidecar` | Python | 8087 | Subscribes to Redis decisions across `smartload.routing`, `.anomaly`, `.policy`, `.scale`; atomically rewrites `upstream.conf`; triggers `nginx -s reload` |
| `telemetry` | Python | 8081 | OTLP ingest + read API over TimescaleDB |
| `anomaly-detector` | Python | 8082 | Threshold baseline (default) + trained Isolation Forest (opt-in via `ANOMALY_ENGINE=isolation_forest`) |
| `forecasting` | Python | 8083 | Moving-average baseline + ARIMA plugin slot |
| `rl-engine` | Python | 8084 | Random-shadow baseline + PPO policy with `shadow`/`active` mode pin |
| `autoscaler` | Python | 8085 | Forecast-driven scale + cooldown + reactive fallback |
| `policy-manager` | Python | 8086 | Operating policy REST API + audit + Redis publish on change |
| `operator-ui` | Flask + React | 8090 | BFF + web transparency / override surface |
| `webhook-dispatcher` | — | — | Outbound HMAC-signed HTTP events (planned) |

Infrastructure: TimescaleDB · Redis · OTel Collector · Prometheus · Grafana — all configured under `infrastructure/`.

### Plugin model

All three AI services (`anomaly-detector`, `forecasting`, `rl-engine`) share the same plugin shape. To add a new engine: drop `services/<svc>/models/<name>.pkl` (or `policy.zip` for RL), implement `engines/<name>/engine.py` (or `policies/<name>/policy.py`) subclassing the service ABC, register the name in the factory, set `<SVC>_ENGINE=<name>` (or `RL_POLICY`) — no service-shell changes. Falls back to the baseline automatically if the artifact is missing.

Same shape for load-balancer adapters: `services/shared/lb_adapters/<name>/` implements the `LoadBalancerAdapter` ABC. NGINX ships; HAProxy / Envoy / ALB are stubbed.

Run-loop knobs:
- `anomaly-detector` — `ANOMALY_RUNLOOP_ENABLED`, `ANOMALY_ENGINE` (`threshold` | `isolation_forest`)
- `forecasting` — `FORECAST_RUNLOOP_ENABLED`, `FORECAST_ENGINE` (`moving_average` | `arima`)
- `rl-engine` — `RL_RUNLOOP_ENABLED`, `RL_POLICY` (`random_shadow` | `ppo`), `RL_MODE` (`shadow` | `active`)

---

## Contracts

Every interface has a single source of truth:

| Surface | Canonical contract | Reference |
|---|---|---|
| HTTP REST | [`docs/openapi/smartload-v1.yaml`](docs/openapi/smartload-v1.yaml) (OpenAPI 3.1) | [SOT §26](docs/SOURCE_OF_TRUTH.html#sec-26-api-guide) |
| Redis pub/sub | [`docs/redis-channels.md`](docs/redis-channels.md) | [SOT §11](docs/SOURCE_OF_TRUTH.html#sec-interface-authority) |
| Webhooks | inline in OpenAPI spec | [SOT §29](docs/SOURCE_OF_TRUTH.html#sec-29-webhooks) |
| Python SDK | `clients/python/smartload_client/` | [SOT §27](docs/SOURCE_OF_TRUTH.html#sec-27-sdk) |
| Database schema | [`infrastructure/timescaledb/init.sql`](infrastructure/timescaledb/init.sql) | [SOT §30](docs/SOURCE_OF_TRUTH.html#sec-30-database) |
| Per-feature manifests | [`docs/features/`](docs/features/) | one file per shipped feature |

Three CI guardrails keep contracts honest:
- `scripts/lint-redis-channels.py` — every Redis channel in source must appear in the registry
- `scripts/lint-openapi.py` — every `/api/v1/*` route in source must appear in the OpenAPI spec
- `scripts/lint-structure.py` — every `tests/e2e/<feature>/` must have a sibling `docs/features/<feature>.md` + `examples/scenarios/<feature>/`

---

## Repository structure

Role-based layout: services mirror deployment topology, plugins live one folder per implementation, feature artefacts triangulate across `docs/features/` + `examples/scenarios/` + `tests/e2e/`.

```
smartload/
├── services/                         # one folder per deployable service
│   ├── load-balancer/                # NGINX
│   ├── lb-otel-shipper/              # NGINX log → OTLP shipper
│   ├── lb-sidecar/                   # dynamic upstream rewriter
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
│   └── helm/smartload/               # Chart.yaml + values.yaml (templates WIP)
├── tests/                            # PEP 420 namespace packages — no __init__.py
│   ├── unit/<service>/               # pure-function tests per service
│   ├── integration/                  # service-pair / contract tests
│   ├── e2e/<feature>/                # feature-level tests via the SDK
│   ├── conformance/lb_adapter/       # every adapter must pass these
│   └── performance/                  # Locust
├── tools/                            # dev-utility containers (not shipped middleware)
│   ├── demo-ui/                      # scenario-injection + chaos + live SSE feed (:8091)
│   └── traffic-simulator/            # Locust UI (:8089)
├── docs/
│   ├── SOURCE_OF_TRUTH.html          # canonical spec (read first)
│   ├── PROJECT_WALKTHROUGH.md        # narrative walkthrough
│   ├── PROJECT_STATE.md              # point-in-time audit of where the project stands
│   ├── features/                     # per-feature manifests
│   ├── architecture/                 # control / data plane, multi-tenancy, failure modes
│   ├── openapi/                      # smartload-v1.yaml
│   ├── planned/                      # placeholder docs for unimplemented services
│   ├── academic-assessment.md        # project provenance + thesis / poster / presentation lift table
│   └── redis-channels.md             # canonical channel registry
├── scripts/                          # lint-*.py, seed-metrics.py, download-datasets.sh
├── config/                           # policy.yaml + .env.example
├── datasets/                         # public training data; fetched, gitignored
├── test-backends/                    # Node.js stubs the autoscaler scales
└── docker-compose.yml
```

Canonical tree with placement rules: [SOT §7](docs/SOURCE_OF_TRUTH.html).

---

## Testing

```bash
pytest tests/unit/                                    # pure-function (no live stack)
docker compose up -d && pytest tests/integration/     # service-pair / wire-protocol
docker compose up -d && pytest tests/e2e/             # feature-level via SDK
pytest tests/conformance/                             # interface conformance (per plugin)
docker compose up traffic-simulator                   # Locust at :8089
```

Structural lints (permissive today; strict mode planned):

```bash
python scripts/lint-structure.py
python scripts/lint-redis-channels.py
python scripts/lint-openapi.py
```

---

## Datasets

Public datasets the ML services train and evaluate against. `scripts/download-datasets.sh` fetches them.

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

---

## Stability and versioning

- **HTTP API** — path versioning (`/api/v1`, `/api/v2`). Breaking changes require a successor path + at least one quarter of overlap, with `Sunset` / `Deprecation` headers on the old surface.
- **`policy.yaml` schema** — `schema_version` field is the migration anchor.
- **Redis envelopes** — every envelope carries a `version` field; subscribers must tolerate unknown fields.
- **Python SDK** — semver. Major bumps mirror API breaking changes.

---

## Roadmap

SmartLoad is currently a single-tenant self-hosted middleware. On deck:

| Track | What's coming |
|---|---|
| **Production hardening** | Helm chart templates, DB migrations folder, end-to-end correlation IDs, AI-service Prometheus `/metrics`, strict structural lint |
| **Load-balancer plugins** | HAProxy, Envoy, AWS ALB adapters behind the existing `LoadBalancerAdapter` ABC |
| **Webhook delivery** | Outbound HMAC-signed HTTP events with at-least-once retries |
| **Operator UI** | Embedded metrics dashboards (no Grafana context switch), service log viewer, named-strategy aliases |
| **Multi-tenant SaaS** | Per-tenant API keys + RBAC, rate limiting, tenant-scoped Redis namespacing — opt-in; single-tenant remains the default shape |

Detailed roadmap: [GitHub milestones](https://github.com/TasneemEltabakh/smartload/milestones) · current state: [`docs/PROJECT_STATE.md`](docs/PROJECT_STATE.md).

---

## Contributing

Issues and pull requests welcome. A few conventions worth knowing before you open a PR:

- Every new feature ships its own `docs/features/<feature>.md` manifest + `tests/e2e/<feature>/` suite + `examples/scenarios/<feature>/` runnable demo. The three structural lints enforce this.
- Architectural decisions go in [`docs/SOURCE_OF_TRUTH.html`](docs/SOURCE_OF_TRUTH.html); narrative implementation context goes in [`docs/PROJECT_WALKTHROUGH.md`](docs/PROJECT_WALKTHROUGH.md). The two stay in sync.
- Run `pytest tests/unit/` before pushing; `compose-test` runs the full e2e + integration suite in CI.

For substantial features, open an issue first so we can shape the slice together.

---

## Getting help

- **Documentation** — [`docs/SOURCE_OF_TRUTH.html`](docs/SOURCE_OF_TRUTH.html) (canonical spec) · [`docs/PROJECT_WALKTHROUGH.md`](docs/PROJECT_WALKTHROUGH.md) (narrative tour) · [`docs/features/`](docs/features/) (per-feature manifests)
- **Bug reports / feature requests** — [GitHub Issues](https://github.com/TasneemEltabakh/smartload/issues)
- **Questions and discussion** — [GitHub Discussions](https://github.com/TasneemEltabakh/smartload/discussions)

---

## License

MIT — see [`LICENSE`](LICENSE).
