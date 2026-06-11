# SmartLoad — State of the project

**Audit date:** 2026-06-09 (last full audit) · **Refreshed:** 2026-06-11 (delta-only)
**Baseline commit:** `e95997d` (v1.0.7ab — #101 N2.1 Isolation Forest engine landed + review fixes)
**Audit method:** four parallel structured audits, one per architectural layer (decision plane, data plane + telemetry, control plane + UI + integration, infra + tests + docs + benchmarks); each layer's actual code verified against SOT §18 Build Status claims; reports synthesised into this document.

**Delta since 2026-06-09 full audit:** v1.0.7w (#159 forecasts hypertable, b3b985f) · v1.0.7x (#156 R2 + #157 R3 adaptive-bench + first run, 49614c0/def8ab0/649ef13) · v1.0.7y (#163 decision-plane catch-all + liveness, 974c56d) · v1.0.7z (#164 lb-sidecar `smartload.scale` subscription closing the autoscaler → NGINX loop, 15922f7) · v1.0.7aa (#103 T2.3 closed-loop integration tests landing as the executable spec for the four-channel dispatch) · **v1.0.7ab (#101 N2.1 Isolation Forest engine shipped, F1=0.8012 > 0.80 SMD-holdout gate, c1a8c1d + e95997d review fixes — `threshold` remains the compose default pending the empirical calibration finding below)**. The six releases together close S5's high-priority items and unblock RQ4 quantitative measurement.

**Empirical finding from v1.0.7ab `anomaly-engine-bench`:** on a synthetic 12×12 sweep of `(latency_ms ∈ [10, 600], error_rate ∈ [0, 0.20])`, the trained Isolation Forest engine agreed with the threshold baseline on only **25% of cells** (36/144). The asymmetry is one-sided: the model said `healthy` on 107 of 108 cells where the threshold rule said `unhealthy`. This is the **domain-adaptation gap** the engine's README (`engines/isolation_forest/README.md`) flagged at PR time, now with numbers: the `production_scaler` (fit on MST-2021 features as a proxy for live telemetry) maps real-millisecond latency into a space where the SMD-trained model's outlier boundary doesn't trigger. The model passes its own training-distribution gate (F1=0.8012 on SMD holdout) but is currently **under-reacting at production scales**. Re-tuning tracked as **#165** (production-scale `production_scaler` re-calibration — acceptance: ≥80% bench agreement + live-stack test passes with `ANOMALY_ENGINE=isolation_forest`); SOT §35.6 carries the architectural-alternative deferral (LSTM-AE) as the fallback path.

> This document is a **point-in-time snapshot** of completeness. It is **not** the canonical product spec — that is [`SOURCE_OF_TRUTH.html`](SOURCE_OF_TRUTH.html). This doc tells you *where the project stands today*; the SOT tells you *what the project is supposed to be*. When the two disagree, the SOT wins as the design authority; this doc gets updated to reflect the new reality.

---

## Executive summary

**Implementation completeness: ~85 %** for the present single-tenant middleware phase.

| Lens | Score | Read |
|---|---|---|
| **Implementation** — code shipped + tested | **85 %** | Every architectural layer is at least at baseline; plugin slots in place; tests comprehensive. |
| **Production maturity** — operationally shippable as middleware | **70 %** | Own-metrics, DB migrations, correlation ID, strict-lint, Helm templates are real gaps for operating it. |
| **Evaluation evidence** — publishable head-to-head numbers | **50 %** | Harnesses + offline eval shipped; the v1.0.7t honest finding is "PPO ties RR on heterogeneous workload"; closing requires retraining (Rghda's track) + adaptive bench (#156 / #157). |

### Headline read

- The codebase is **product-shippable** at the scope it was scoped for. Six sprints in, every service has a Phase-1 run loop wired and enabled by default, with deterministic fallbacks at every layer.
- The **decision plane works mechanically**: anomaly + forecast + RL all publish envelopes; the lb-sidecar reroutes; the autoscaler scales. v1.0.7v added create/destroy capability to the autoscaler. The flow is unbroken end-to-end.
- The **honest evaluation gap** is the v1.0.7t finding: PPO was trained on homogeneous Alibaba traces and does not yet beat NGINX round-robin on the heterogeneous bench. This is recorded in SOT §34 Results and tracked under §34.6 closing-the-gap deliverable. The mechanism is sound; the trained model is the binding constraint.
- **Phase 2 SaaS items** (multi-tenancy, RBAC, rate limiting, webhook dispatcher, auth) are explicitly deferred and **not counted** against completeness — they are scope decisions, not gaps.

---

## By-layer breakdown

### Data plane + telemetry — 95 %

NGINX serves traffic over the 5-backend test pool with `proxy_next_upstream` + `max_fails`. The lb-otel-shipper tails the JSON access log and POSTs OTLP/HTTP-JSON to the OTel Collector, which forwards to the telemetry service. Telemetry writes to TimescaleDB via the canonical `METRICS_INSERT` constant in `shared/queries.py`. The lb-sidecar consumes Redis envelopes across **four channels** — `smartload.routing` + `smartload.anomaly` + `smartload.policy` + `smartload.scale` (v1.0.7z, #164 closes the autoscaler → NGINX loop) — and atomically rewrites `upstream.conf` + triggers `nginx -s reload`. **Per-request fidelity is verified** at every layer by an integration test asserting `STDDEV(request_latency_ms) > 0` on live traffic.

**One acknowledged gap:** AI services expose `/health` (JSON) only, not Prometheus `/metrics` (text format). Only the OTel Collector exposes scrapable Prometheus metrics on `:8889`. Operators rely on TimescaleDB-backed Grafana panels rather than Prometheus dashboards for service-internal observability.

### Decision plane — 82 %

Four services, all wired:

| Service | State |
|---|---|
| **anomaly-detector** | Threshold engine ships as baseline (compose default); Phase-1 run loop enabled by default; `/api/v1/isolate` manual endpoint (slice #3) wired. **Isolation Forest plugin shipped v1.0.7ab** (#101) — trained on SMD with F1=0.8012 on holdout (PASS of >0.80 KPI gate), but the comparison bench at `experiments/anomaly-engine-bench/` shows only 25% agreement with threshold at production scales (production_scaler domain-adaptation gap — root cause: SMD-trained model lives in a different mean-0/std-1 coordinate system than the one production_scaler maps real-ms inputs into). Re-calibration tracked as **#165**; threshold remains compose default until that lands. |
| **forecasting** | Moving-average baseline + ARIMA(3,0,1) artifact (36.9 MB `arima_model.pkl`) both shipped. ARIMA currently measures **25 % MAPE** — the SOT KPI is **< 20 %**; `moving_average` therefore remains the default until tuning closes the gap (§35.2). |
| **rl-engine** | Random-shadow baseline + PPO policy (`policy.zip`, 156 KB) + four classical baselines (round_robin, least_connections, random_shadow). Anomaly-aware action-space filtering wired; `RL_MODE=shadow` is the safety pin (operator must explicitly opt in to `active`). Offline eval shows PPO ties round_robin on homogeneous Alibaba traces; v1.0.7t bench confirms the same on the heterogeneous workload. |
| **autoscaler** | T1.3 + T1.4 wired (forecast subscriber + Docker SDK scale + cooldown + reactive fallback + policy live reload + `/api/v1/audit/scaling` + `/api/v1/scale` manual). **v1.0.7v added** `provision()` / `decommission()` lifecycle pair behind `AUTOSCALER_PROVISIONING_ENABLED` feature flag (OFF by default; #156 will flip it ON for the adaptive bench). |

**Material gaps**: ARIMA misses its KPI; Isolation Forest is scaffolded; PPO needs retraining on heterogeneous traces (the binding constraint per §34.6). None of the four services expose own-metrics in Prometheus format.

### Control plane + UI + integration — 88 %

`policy-manager` is fully shipped (T1.4): GET / POST with strict body validation (v1.0.7p closes #152), atomic YAML write, `policy_changes` audit per field, envelope publish on `smartload.policy`, idempotent no-op detection. 38 unit + 4 integration tests.

`operator-ui` ships **5 pages**: Home, Policy, Audit, Actions, Live Engines. All five routes have backing BFF endpoints and React/Vite frontend pages. The SSE stream at `/api/ui/engines/stream` carries all four decision-plane channels merged with a 256-item per-client queue. Consolidated `/api/v1/status` (#149, v1.0.7q) gives a one-call read across all services with a 2-second per-service timeout.

`demo-ui` (v1.0.7s) ships **4 pages**: Overview, Controls, Feed, Benchmark. The Benchmark page reads run history from `experiments/baseline-vs-smartload/results/`.

**Python SDK** (`clients/python/smartload_client/`): `PolicyClient` + `StatusClient` + `ActionsClient` + `AuditClient` + `EnginesClient` + `EventsClient` are all real. `MetricsClient` (#127) and `WebhooksClient` (#130) are stubbed `NotImplementedError`s — deferred Phase 2.

**Shared layer** (`services/shared/`): `contracts.py` includes the v1.0.7v `ScalingEvent.mechanism` field; the NGINX lb-adapter is fully implemented; ALB / Envoy / HAProxy adapter slots exist as 22-line stubs.

**Webhook dispatcher**: no service folder yet — `docs/planned/webhook-dispatcher.md` is the tracking doc; #130 lands the implementation.

### Infra + tests + docs + benchmarks — 87 %

All 5 Grafana dashboards (Overview + RL Routing + Anomaly + Scaling + Forecast) ship and load on stack-up. The Forecast dashboard's predicted-RPS sparse-line gap (§35.8) was **closed in v1.0.7w (#159)** — forecasts now land in a `forecasts` hypertable on every publish.

Helm chart at `infrastructure/helm/smartload/` is scaffold-only: `Chart.yaml` + `values.yaml` are complete; `templates/` contains `.gitkeep` only. Raw K8s manifests at `infrastructure/k8s/` are placeholder.

The **#148 baseline-vs-SmartLoad bench harness** at `experiments/baseline-vs-smartload/` ships with two SHORT-mode runs in `results/`. The full-length 6-min/side run on a retrained PPO model is the outstanding deliverable. The **adaptive bench** (`experiments/adaptive-bench/`) **shipped end-to-end** in v1.0.7x: R1 dynamic-pool foundation (#155, 96d1992), R2 orchestrator + collectors + 5-phase Locust shape (#156, 49614c0), R3 analysis pipeline + 4 plots + SUMMARY.md (#157, def8ab0). First end-to-end run also recorded; the post-#163/#164 rerun will produce affirmative gate strings for "pool grew during B" / "pool shrank during D".

CI shipping: lint + unit-tests + build-services matrix (8 services) + runtime-import-smoke + compose-test. Three structural lints (`lint-structure.py`, `lint-redis-channels.py`, `lint-openapi.py`) ship in permissive mode (#139 flips them to enforcing).

Docs: 8 feature manifests under `docs/features/` (policy / audit / manual-actions / status / lb-sidecar / anomaly-detection / forecast-autoscale / live-engines). Architecture docs (`control-plane.md`, `data-plane.md`) shipped; `lb-adapter.md`, `failure-modes.md`, `versioning-policy.md`, `multi-tenancy.md` planned but not yet written. SOT §§31–35 (Background / Algorithms / Methodology / Results / Limitations) shipped v1.0.7u. PROJECT_WALKTHROUGH §8 (algorithms + training procedure) shipped v1.0.7u. README has a "Writing about SmartLoad" pointer block.

---

## What's MISSING (not yet started)

| Item | Issue | Severity | Owner |
|---|---|---|---|
| ~~Adaptive-bench Round 2~~ | ~~#156~~ | **CLOSED** v1.0.7x (49614c0) | — |
| ~~Adaptive-bench Round 3~~ | ~~#157~~ | **CLOSED** v1.0.7x (def8ab0) | — |
| Webhook dispatcher service | #130 | Medium — placeholder doc exists | — |
| Helm chart templates | #133 | Medium — required for K8s HPA comparison | — |
| Raw K8s manifests | — | Low — explicitly Phase 2 | — |
| Redis exporter to Prometheus | #116 (moved to S5) | Low — operational maturity | — |
| AI-service `/metrics` endpoints (Prometheus format) | #161 | Medium — closes observability gap | — |
| Architecture docs: `lb-adapter.md`, `failure-modes.md`, `versioning-policy.md`, `multi-tenancy.md` | #162 | Low — incremental as features stabilise | — |
| Multi-run bench batching with per-metric CIs | #160 | **High** — biggest mover for publishable-evidence lens; harness now unblocked | — |

## What's STUB ONLY (scaffolded, no implementation)

| Item | Where | Status |
|---|---|---|
| ALB load-balancer adapter | `services/shared/lb_adapters/alb/` | 22-line `NotImplementedError` stub on every method |
| Envoy load-balancer adapter | `services/shared/lb_adapters/envoy/` | 22-line `NotImplementedError` stub |
| HAProxy load-balancer adapter | `services/shared/lb_adapters/haproxy/` | 22-line `NotImplementedError` stub (issue #147) |
| SDK `MetricsClient` | `clients/python/smartload_client/metrics.py` | `NotImplementedError` pending #127 |
| SDK `WebhooksClient` | `clients/python/smartload_client/webhooks.py` | `NotImplementedError` pending #130 |
| K8s manifests | `infrastructure/k8s/` | `.gitkeep` only |
| Helm chart templates | `infrastructure/helm/smartload/templates/` | `.gitkeep` only |

## What's INCOMPLETE (real implementation, with material gaps)

| Item | Gap | What closes it |
|---|---|---|
| **ARIMA forecaster** | 25 % MAPE vs KPI < 20 %; `moving_average` stays default | Hyperparameter tuning + extended training window (Nada; §35.2) |
| **PPO routing policy** | Trained on homogeneous Alibaba; ties RR on the v1.0.7t heterogeneous bench | Retraining on heterogeneous traces (Rghda; §34.6 binding constraint) |
| **Isolation Forest anomaly engine** | F1=0.8012 on SMD holdout (PASS) but 25% bench agreement at production scales — model is in standardised-SMD coordinates, inputs arrive in standardised-MST coordinates, two unrelated mean-0/std-1 spaces (#101 shipped infra, calibration is the gap) | Re-calibrate `production_scaler` on real production telemetry or re-fit IF on production-shape features directly (#165 — acceptance: ≥80% bench agreement + live-stack test passes with `ANOMALY_ENGINE=isolation_forest`) |
| **Baseline-vs-SmartLoad bench (#148)** | Only SHORT-mode runs (~2 min/side); full-length (~6 min/side) on retrained PPO owed | Re-run after retraining + multi-run batching with CIs (§35.3 — #160) |
| **Anomaly + Forecast scenario walks** | Manifests + e2e tests exist; standalone `examples/scenarios/<feature>/` walk scripts do not | 1–2 hours each |

## What's WRONG (incorrect implementation)

**Nothing flagged.** Every SOT §18 claim verified by the audit matched the actual code. The closest items to "wrong" are documentation-side:

- The #155 original issue body said the BFF SSE endpoint was `/api/ui/events` — the actual endpoint is `/api/ui/engines/stream`. (Issue text was wrong; code is right; corrected in #156.)
- The `scaling_events.action` SQL column carries `"scale_out" | "scale_in"` text; v1.0.7v's new `mechanism` field rides in the envelope and textually in `reason` rather than a structured column. Defensible (no migration needed) but a future column add would let downstream consumers join on it cleanly.

## What NEEDS ENHANCEMENT (works today, could be production-grade)

| Area | Enhancement | Issue |
|---|---|---|
| Own-metrics | Add Prometheus-format `/metrics` to each AI service (publish count, cycle latency, decision distributions) | #161 |
| API versioning + deprecation | Formal `Sunset` / `Deprecation` header window mechanism | #134 |
| Strict lint mode | Flip the three structural lints from permissive to enforcing | #139 |
| DB migrations | Migrations folder + first migration script (today's ops rely on `init.sql` idempotency) | #141 |
| Request correlation ID | W3C Trace Context end-to-end propagation for per-request explainability | #143 |
| Test reorg | Migrate applicable `tests/integration/*` into `tests/e2e/<feature>/` | #140 |
| Backup / restore runbook | TimescaleDB backup story not yet documented | #142 |
| Multi-run bench batching | Single-run point estimates today; multi-run + per-metric CIs make results publishable | §35.3 — #160 |

## What's DEFERRED (Phase 2 SaaS — not a gap)

Per SOT §35.1 — these are explicit Phase 2 SaaS items, not unfinished work:

- Multi-tenancy + `tenant_id` plumbing (#129)
- Tenant API keys + RBAC (#132)
- Rate limiting + per-token quotas (#135)
- Operator UI authentication (#125, OUI.7)
- TLS termination at the LB
- Per-tenant Redis namespacing
- Managed-SaaS control plane

---

## Honest evaluation verdict

The code is product-shippable at ~85 % for the present phase. The harder honest call lives in SOT §34 Results:

> **The harness works; the lb-sidecar mechanism works; the PPO model has not been trained for the workload the bench exposes.** v1.0.7t per-phase p95 numbers: baseline 14 / 42 / 44 / 43 ms; SmartLoad 23 / 41 / 50 / 44 ms across A_ramp / A_hold / B_anomaly / C_sustain. SmartLoad's max latency 3,082 ms vs baseline 150 ms is the lb-sidecar's NGINX-reload cost during the anomaly window.

This is the most important thing to internalize: the gap between **code shipped** and **evidence shipped** is real and larger than the % suggests. Closing it requires three concrete deliverables:

1. **Retrained PPO** on heterogeneous-latency training distribution (Rghda's workstream)
2. **Full-length bench rerun** after retraining (the v1.0.7r outstanding item)
3. **Adaptive bench** (#156 + #157) to answer RQ4 quantitatively

None of these change the architecture; all of them close measurable, named gaps.

---

## Bottom line by lens

| If "the project" means… | Score | What gets you to 90 %+ |
|---|---|---|
| **The codebase** (services + tests + docs) | **~88 %** (was 85) | #156 / #157 / #159 / #163 / #164 all closed in v1.0.7w–z. Next: #103 T2.3 e2e tests + #160 multi-run CIs — 90 %+ |
| **Production-ready middleware** | **70 %** | + own-metrics + #141 migrations + #143 correlation IDs + #139 strict lint + Helm templates — 85 % |
| **Publishable evidence** | **~55 %** (was 50) | Adaptive-bench harness shipped; needs the post-#163/#164 rerun + retrained PPO + multi-run CIs (#160) — 75 % |

If you only count what is *currently shipped against the current-phase scope*, SmartLoad is a defensible product foundation. The known gaps are named, owned, and traceable to specific issues. There is no zombie surface area — every stub has either an issue number or an explicit Phase 2 deferral.

---

## Sprint state at audit time

| Sprint | Period | Phase | Open issues | Status |
|---|---|---|---|---|
| S1 | Feb 1 – Apr 24 | Phase 0 | 0 | DONE |
| S2 | Apr 28 – May 9 | Phase 1A | 0 | DONE |
| S3 | May 10 – May 23 | Phase 1B | 0 | DONE |
| S4 | May 24 – Jun 6 | Phase 2 | **5** | Carry-forward Rghda + Nada workstreams (#7 NAB/Yahoo SMD partial, #98, #99, #104, #118; #101 closed v1.0.7ab) |
| S5 | Jun 7 – Jun 20 | Phase 3A | **2** | Active — #117 acceptance pattern + #116 Redis exporter (#101 closed v1.0.7ab; #103 closed v1.0.7aa; #159 / #163 / #164 closed mid-sprint) |
| S6 | Jun 21 – Jun 30 | Phase 3B (impl) | **24** | Implementation & release hardening — feature delivery (#130 webhooks, #131 OUI.8, #133 Helm, #124/#125 OUI.6/.7, #56 auth model), production maturity (#139 strict lint, #140 test reorg, #141 migrations, #143 correlation IDs, #161 /metrics, #134 versioning + deprecation, #142 backup runbook), integration adoptions (#145/#146/#147/#150), regression + release (#37, #42, #43, #46, #126), final bench + multi-run CIs (#39, #160) |
| S7 | TBD (follows S6) | Phase 3C (docs) | **7** | Final report & presentation — pure prose deliverables (#16, #21, #40, #44, #45, #162) + demo script & slides (#41). Split out from legacy S6 on 2026-06-11 so 0% on S7 with S6 done = code complete, writeup remaining |

**Phase 2 — SaaS adaptation** (no sprint, explicit deferral per SOT §25): **3 open** — #129 multi-tenancy, #132 tenant API keys + RBAC, #135 rate limiting. Not counted against present-phase completeness; these are scope decisions, not gaps. Milestone created 2026-06-11 to make the deferral explicit (was previously implicit via "no milestone").

Total open issues at 2026-06-11 refresh: **43** (6 S4 + 3 S5 + 24 S6 + 7 S7 + 3 Phase 2). The 2026-06-11 retriage moved 16 previously unmilestoned issues into buckets (13 → S6, 3 → new Phase 2 milestone) and split the legacy "Sprint 6 — Final Report & Presentation" into S6 (implementation & release hardening) + S7 (docs-only — 7 prose deliverables peeled off from the old S6) so docs progress no longer masks impl completion.

---

## How to refresh this doc

Re-run the audit when:

- A major release lands (e.g. #156 R2 closes — re-audit infra layer)
- A new architectural layer is added
- A sprint boundary passes
- Sign-off / external review is needed

The methodology is reproducible: four parallel audits (decision plane / data plane / control plane / infra) read SOT §18 Build Status claims and verify each against the actual code, then merge their reports into this document. The most recent baseline commit at the top of this file is the canonical anchor.
