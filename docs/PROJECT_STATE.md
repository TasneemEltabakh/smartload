# SmartLoad — State of the project

**Audit date:** 2026-06-09 (last full audit) · **Refreshed:** 2026-06-13 (delta-only)
**Baseline commit:** `7f044a3` (v1.0.7ak — live-stack test pins routing itself)
**Audit method:** four parallel structured audits, one per architectural layer (decision plane, data plane + telemetry, control plane + UI + integration, infra + tests + docs + benchmarks); each layer's actual code verified against SOT §18 Build Status claims; reports synthesised into this document.

**Delta since 2026-06-09 full audit:** v1.0.7w (#159 forecasts hypertable, b3b985f) · v1.0.7x (#156 R2 + #157 R3 adaptive-bench + first run, 49614c0/def8ab0/649ef13) · v1.0.7y (#163 decision-plane catch-all + liveness, 974c56d) · v1.0.7z (#164 lb-sidecar `smartload.scale` subscription closing the autoscaler → NGINX loop, 15922f7) · v1.0.7aa (#103 T2.3 closed-loop integration tests landing as the executable spec for the four-channel dispatch) · v1.0.7ab (#101 N2.1 Isolation Forest engine shipped, F1=0.8012 > 0.80 SMD-holdout gate, c1a8c1d + e95997d review fixes — `threshold` remains the compose default pending the empirical calibration finding below) · v1.0.7ac (#116 Redis exporter + Grafana control-bus dashboard + SOT §8.10 publish-rate budgets, per-channel design targets with aggregate `>50 ops/sec` saturation alert threshold) · v1.0.7ad (#117 per-task acceptance-test pattern + tests/README.md + PR template + #7 closed-with-rationale — S5 fully closed) · **v1.0.7ae (doc-completeness sweep: 8 SOT figures updated to cover redis-exporter / smartload.scale subscribers / forecasts+policy_changes hypertables, 3 new walkthrough Mermaid blocks for lb-sidecar dispatch + IF scoring-coordinate-bridge + test-layer pyramid, §13 Tech Stack flipped from pending to shipped for Anomaly model / Dashboards / Operator UI, README + redis-channels brought into line — no hidden modules remain across the canonical docs)**. The nine releases together close all S5 high-priority items, unblock RQ4 quantitative measurement, and bring docs back into full agreement with codebase reality.

**Delta — 2026-06-13 session (v1.0.7af → ak):** **v1.0.7af** (demo-ui redesigned from 4 decision-centric pages into a 5-page developer **Dev Console** — Dashboard / Benchmarks (both suites) / Run (one-click in-cluster load profiles + live monitor) / Controls / Live Feed; SUMMARY-parsed KPI strip) · **v1.0.7ag** (Run-page run history + side-by-side compare + lost-run/stale detection) · **v1.0.7ah → ak: #165 calibration closed** (compose default later reverted — see next delta) — the Isolation Forest engine was re-calibrated in production-shape space (`train_production.py`), lifting `anomaly-engine-bench` agreement **25% → 91.4%** with zero under-reactions (`ah`, `c4a5fc6`); the lb-otel-shipper now reverse-resolves backend IPs → canonical container names, closing the live-stack test (`ai`, `c7c4c81`); the compose default flipped to `isolation_forest` (`aj`, `0c52c22`); and the live-stack test was hardened to pin routing itself, RL-policy-independent (`ak`, `7f044a3`). Also this session: the **first committed adaptive-bench RQ4 run** (`experiments/adaptive-bench/results/20260612T162342Z/`) — pool **1 → 6** under forecast-driven scale-out, then back down; 12 scaling actions; surfaced live in the Dev Console's Benchmarks page.

**Delta — later 2026-06-13:** **v1.0.7al** (#161 AI-service Prometheus `/metrics` own-metrics, 0e1c1bc) · **v1.0.7am** (#160 multi-run bench batching with per-metric CIs — both harnesses take `--runs N`/`RUNS=N` (default 5) + a new shared `experiments/_bench_common/bench_stats.py` (Student's t) + per-harness `scripts/aggregate_runs.py` → `summary.parquet` + `mean ± CI` `SUMMARY.md` + CI-band plots; the baseline plotter's empty-plot column bug fixed en route; §35.3 capability closed). **Operational note from the #160 live smoke:** the isolation_forest anomaly engine (compose default v1.0.7aj) over-excluded the single seed backend under bench load → the LB 502'd on the whole pool (the residual risk this doc flagged, now confirmed). **v1.0.7an reverted the compose default back to `threshold`** so the stack is safe out of the box while the over-exclusion is fixed (follow-up to #165); opt back in with `ANOMALY_ENGINE=isolation_forest`. The model + serving plugin are unchanged.

**Delta — 2026-06-13 pre-Sprint-6 hardening (v1.0.7ap → ar):** the over-exclusion follow-up is addressed structurally — **v1.0.7ap** added a lb-sidecar quorum guard + NGINX adapter safety net so an exclusion can never empty the pool (removing the 502 feedback loop, the root cause of the v1.0.7an revert; it protects the `threshold` engine too); **v1.0.7aq** guards non-finite (NaN/inf) anomaly features that had been scoring a spurious `unhealthy`; **v1.0.7ar** moved all 7 control-plane Flask services off the single-threaded Werkzeug dev server to waitress (production WSGI). Bug/hardening fixes only — no new features; unit suites green (lb-sidecar 71, anomaly-detector 32). Re-enabling `isolation_forest` as the compose default is now gated only on a live-stack smoke. (Branch `feat/pre-s6-hardening`; checkpoint before the cross-cutting refactors.)

**Delta — v1.0.7ao (2026-06-13):** Doc↔code credibility reconciliation (no feature-behaviour change). (1) The operator-ui BFF's `ui_metrics_ops()` now wires `throughput_rpm`/`requests_total` from telemetry's already-shipped `/api/v1/metrics/rpm` — they were hardcoded `null` behind a stale `TODO`, so the Home-page KPIs had rendered "—" for resolvable data. (2) The operator-ui README's false "Scaffolded only… does not run" status was corrected (the service is built + running on `:8090`). (3) The stale present-tense "isolation_forest is the compose default / #165 fully closed" summary claims in **this document** were reconciled to the v1.0.7an reality (calibration closed at 91.4%; compose default `threshold`; opt-in via `ANOMALY_ENGINE=isolation_forest`) — the SOT (§18/§22), README, WALKTHROUGH, and feature docs were already accurate, so PROJECT_STATE was the lone straggler. The single most-leveraged *next* move stands unchanged: the binding constraint is evaluation evidence (~58%), not code (~88%) — settle the RL/PPO verdict and run the full-length multi-run adaptive bench.

**Empirical finding from v1.0.7ab `anomaly-engine-bench`:** on a synthetic 12×12 sweep of `(latency_ms ∈ [10, 600], error_rate ∈ [0, 0.20])`, the trained Isolation Forest engine agreed with the threshold baseline on only **25% of cells** (36/144). The asymmetry is one-sided: the model said `healthy` on 107 of 108 cells where the threshold rule said `unhealthy`. This is the **domain-adaptation gap** the engine's README (`engines/isolation_forest/README.md`) flagged at PR time, now with numbers: the `production_scaler` (fit on MST-2021 features as a proxy for live telemetry) maps real-millisecond latency into a space where the SMD-trained model's outlier boundary doesn't trigger. The model passes its own training-distribution gate (F1=0.8012 on SMD holdout) but is currently **under-reacting at production scales**. Re-tuning tracked as **#165**; SOT §35.6 carries the architectural-alternative deferral (LSTM-AE) as the fallback path. **Resolved v1.0.7ah (2026-06-13):** re-calibrated in production-shape space (`tools/anomaly-training/train_production.py`, option 3) — scaler + model co-located in one real-ms coordinate system, removing the bridge. Bench agreement **25% → 91.4%** (234/256), **zero under-reactions**; the model now reacts live (publishes `unhealthy` on a 400 ms injection). Two bugs were fixed en route: the model was never copied into the anomaly-detector image (so the engine always fell back to threshold), and the live-stack test surfaced a separate `backend_pool` backend-id mismatch — the lb-otel-shipper labeled `metrics.instance` with NGINX's backend IP rather than the canonical container name. Both fixed: v1.0.7ai added IP→container-name reverse resolution in the shipper (closing the live-stack test), v1.0.7aj flipped the compose default to `isolation_forest`, and v1.0.7ak made the live-stack test pin routing so it's independent of the RL policy. **#165's calibration is closed — all 5 acceptance criteria + the bonus met as of v1.0.7ak.** The #160 live smoke then exposed that the IF default over-excludes the single seed backend under sustained load (LB 502 on the whole pool), so **v1.0.7an reverted the compose default to `threshold`** pending an over-exclusion fix (follow-up to #165); opt back in via `ANOMALY_ENGINE=isolation_forest`.

> This document is a **point-in-time snapshot** of completeness. It is **not** the canonical product spec — that is [`SOURCE_OF_TRUTH.html`](SOURCE_OF_TRUTH.html). This doc tells you *where the project stands today*; the SOT tells you *what the project is supposed to be*. When the two disagree, the SOT wins as the design authority; this doc gets updated to reflect the new reality.

---

## Executive summary

**Implementation completeness: ~88 %** for the present single-tenant middleware phase.

| Lens | Score | Read |
|---|---|---|
| **Implementation** — code shipped + tested | **88 %** (was 85) | Every architectural layer is at least at baseline; plugin slots in place; tests comprehensive. This session: the trained anomaly engine was re-calibrated to 91.4% agreement (#165 calibration closed) — briefly the compose default, then reverted to `threshold` v1.0.7an after the #160 smoke exposed over-exclusion (opt-in via `ANOMALY_ENGINE=isolation_forest`); and the demo-ui became a real developer cockpit. |
| **Production maturity** — operationally shippable as middleware | **70 %** | Own-metrics, DB migrations, correlation ID, strict-lint, Helm templates are real gaps for operating it. |
| **Evaluation evidence** — publishable head-to-head numbers | **58 %** (was 50) | The **adaptive-bench RQ4 loop is now demonstrated with a committed run** (pool 1→6 under forecast, then back down) and surfaced in the Dev Console; the anomaly engine has a 91.4% agreement number. Still open: the PPO-beats-RR claim needs the retrained model (on another machine) + the full-length baseline rerun + multi-run CIs (#160). |

### Headline read

- The codebase is **product-shippable** at the scope it was scoped for. Six sprints in, every service has a Phase-1 run loop wired and enabled by default, with deterministic fallbacks at every layer.
- The **decision plane works mechanically**: anomaly + forecast + RL all publish envelopes; the lb-sidecar reroutes; the autoscaler scales. v1.0.7v added create/destroy capability to the autoscaler. The flow is unbroken end-to-end. The **trained Isolation Forest anomaly engine was re-calibrated this session** (reacts live, agrees 91.4% with the threshold rule while catching variance/spikes it misses); it shipped as the compose default in v1.0.7aj but was **reverted to `threshold` in v1.0.7an** after the #160 smoke exposed over-exclusion under load — available opt-in via `ANOMALY_ENGINE=isolation_forest`.
- The **honest evaluation gap** is the v1.0.7t finding: PPO was trained on homogeneous Alibaba traces and does not yet beat NGINX round-robin on the heterogeneous bench. This is recorded in SOT §34 Results and tracked under §34.6 closing-the-gap deliverable. The mechanism is sound; the trained model is the binding constraint.
- **Phase 2 SaaS items** (multi-tenancy, RBAC, rate limiting, webhook dispatcher, auth) are explicitly deferred and **not counted** against completeness — they are scope decisions, not gaps.

---

## By-layer breakdown

### Data plane + telemetry — 95 %

NGINX serves traffic over the 5-backend test pool with `proxy_next_upstream` + `max_fails`. The lb-otel-shipper tails the JSON access log and POSTs OTLP/HTTP-JSON to the OTel Collector, which forwards to the telemetry service. Telemetry writes to TimescaleDB via the canonical `METRICS_INSERT` constant in `shared/queries.py`. The lb-sidecar consumes Redis envelopes across **four channels** — `smartload.routing` + `smartload.anomaly` + `smartload.policy` + `smartload.scale` (v1.0.7z, #164 closes the autoscaler → NGINX loop) — and atomically rewrites `upstream.conf` + triggers `nginx -s reload`. **Per-request fidelity is verified** at every layer by an integration test asserting `STDDEV(request_latency_ms) > 0` on live traffic. As of **v1.0.7ai** the shipper reverse-resolves NGINX's `$upstream_addr` (a backend IP) to the canonical `<container-name>:<port>` before labeling `metrics.instance`, so the anomaly/RL backend_ids derived from it now match the seed names every channel uses (removing the IP-vs-name impedance the demo-ui's `_ip_to_name_map` was working around).

**Own-metrics — closed v1.0.7al (#161):** the six control-plane services (anomaly-detector, forecasting, rl-engine, autoscaler, policy-manager, lb-sidecar) now expose Prometheus `/metrics` (shared `services/shared/metrics.py`: `<svc>_up`/`_cycle_*`/`_publish_*` + per-service decision counters), scraped by Prometheus and rendered on the Overview dashboard. The per-process surface is now distinct from the per-request TimescaleDB telemetry path. (telemetry itself stays on `/health` until it grows a surface.)

### Decision plane — 86 %

Four services, all wired:

| Service | State |
|---|---|
| **anomaly-detector** | Threshold engine ships as the deterministic baseline + fallback (no longer the compose default); Phase-1 run loop enabled by default; `/api/v1/isolate` manual endpoint (slice #3) wired. **Isolation Forest plugin shipped v1.0.7ab** (#101) — trained on SMD with F1=0.8012 on holdout (PASS of >0.80 KPI gate), then **re-calibrated in production-shape space v1.0.7ah** (#165, `train_production.py`) — bench agreement **91.4%** (was 25%), zero under-reactions, model reacts live; also fixed the model never being copied into the image (v1.0.7ah) and the IP-vs-name `metrics.instance` mismatch (v1.0.7ai). `isolation_forest` was the compose default v1.0.7aj → **reverted to `threshold` v1.0.7an** after the #160 smoke confirmed it over-excludes the single seed backend under load (LB 502); opt back in with `ANOMALY_ENGINE=isolation_forest` once that's fixed (follow-up to #165). The threshold rule is also the automatic fallback if the `.pkl` won't load. |
| **forecasting** | Moving-average baseline + ARIMA(3,0,1) artifact (36.9 MB `arima_model.pkl`) both shipped. ARIMA currently measures **25 % MAPE** — the SOT KPI is **< 20 %**; `moving_average` therefore remains the default until tuning closes the gap (§35.2). |
| **rl-engine** | Random-shadow baseline + PPO policy (`policy.zip`, 156 KB) + four classical baselines (round_robin, least_connections, random_shadow). Anomaly-aware action-space filtering wired; `RL_MODE=shadow` is the safety pin (operator must explicitly opt in to `active`). Offline eval shows PPO ties round_robin on homogeneous Alibaba traces; v1.0.7t bench confirms the same on the heterogeneous workload. **A retrained PPO model is in progress on a separate machine** (Rghda's workstream) — the running stack uses the committed `policy.zip`; the demo-ui/bench routing numbers will move when it lands. |
| **autoscaler** | T1.3 + T1.4 wired (forecast subscriber + Docker SDK scale + cooldown + reactive fallback + policy live reload + `/api/v1/audit/scaling` + `/api/v1/scale` manual). **v1.0.7v added** `provision()` / `decommission()` lifecycle pair behind `AUTOSCALER_PROVISIONING_ENABLED` feature flag (OFF by default; #156 will flip it ON for the adaptive bench). |

**Material gaps**: ARIMA misses its KPI (`moving_average` stays default); PPO needs retraining on heterogeneous traces (the binding constraint per §34.6 — in progress elsewhere). _(Isolation Forest calibration closed — #165, but the compose default was reverted to `threshold` v1.0.7an pending an over-exclusion fix; AI-service Prometheus own-metrics closed — #161, v1.0.7al.)_

### Control plane + UI + integration — 90 %

`policy-manager` is fully shipped (T1.4): GET / POST with strict body validation (v1.0.7p closes #152), atomic YAML write, `policy_changes` audit per field, envelope publish on `smartload.policy`, idempotent no-op detection. 38 unit + 4 integration tests.

`operator-ui` ships **5 pages**: Home, Policy, Audit, Actions, Live Engines. All five routes have backing BFF endpoints and React/Vite frontend pages. The SSE stream at `/api/ui/engines/stream` carries all four decision-plane channels merged with a 256-item per-client queue. Consolidated `/api/v1/status` (#149, v1.0.7q) gives a one-call read across all services with a 2-second per-service timeout.

`demo-ui` (v1.0.7af) was redesigned into a developer **Dev Console** with **5 pages**: Dashboard (stack-health grid + live session metrics + decision card), Benchmarks (suite-aware — surfaces **both** `experiments/adaptive-bench/results/` (RQ4) and `experiments/baseline-vs-smartload/results/`, with manifest facts + a Headline-results KPI strip parsed from SUMMARY + plots + SUMMARY), Run (one-click load profiles driven in-cluster over HTTP with a live RPS/pool/p95 monitor + **run history & side-by-side compare** and lost-run detection, v1.0.7ag), Controls (manual ops), and Live Feed (SSE — now incl. `smartload.scale`). The one-click runner reproduces the adaptive-bench 5-phase shape without the host-side orchestrator (the live autoscaler reacts within the compose pool 1..5); the canonical publishable artefacts still come from `run.py` and are surfaced read-only.

**Python SDK** (`clients/python/smartload_client/`): `PolicyClient` + `StatusClient` + `ActionsClient` + `AuditClient` + `EnginesClient` + `EventsClient` are all real. `MetricsClient` (#127) and `WebhooksClient` (#130) are stubbed `NotImplementedError`s — deferred Phase 2.

**Shared layer** (`services/shared/`): `contracts.py` includes the v1.0.7v `ScalingEvent.mechanism` field; the NGINX lb-adapter is fully implemented; ALB / Envoy / HAProxy adapter slots exist as 22-line stubs.

**Webhook dispatcher**: no service folder yet — `docs/planned/webhook-dispatcher.md` is the tracking doc; #130 lands the implementation.

### Infra + tests + docs + benchmarks — 87 %

All 5 Grafana dashboards (Overview + RL Routing + Anomaly + Scaling + Forecast) ship and load on stack-up. The Forecast dashboard's predicted-RPS sparse-line gap (§35.8) was **closed in v1.0.7w (#159)** — forecasts now land in a `forecasts` hypertable on every publish.

Helm chart at `infrastructure/helm/smartload/` is scaffold-only: `Chart.yaml` + `values.yaml` are complete; `templates/` contains `.gitkeep` only. Raw K8s manifests at `infrastructure/k8s/` are placeholder.

The **#148 baseline-vs-SmartLoad bench harness** at `experiments/baseline-vs-smartload/` ships with two SHORT-mode runs in `results/`. The full-length 6-min/side run on a retrained PPO model is the outstanding deliverable. The **adaptive bench** (`experiments/adaptive-bench/`) **shipped end-to-end** in v1.0.7x: R1 dynamic-pool foundation (#155, 96d1992), R2 orchestrator + collectors + 5-phase Locust shape (#156, 49614c0), R3 analysis pipeline + 4 plots + SUMMARY.md (#157, def8ab0). **The post-#163/#164 RQ4 run is now done** (this session, `results/20260612T162342Z/`): a full 6-min run produced the affirmative result — the pool **grew 1 → 6** under the forecast-driven burst/sustain and shrank back on the drop, across 12 scaling actions (time-to-react 0.6–22 s), with the SUMMARY + 4 plots committed-visible and surfaced in the Dev Console. (Bench result dirs are gitignored; the numbers live in the changelog + the Dev Console's KPI strip.)

CI shipping: lint + **structure-lint** (v1.0.7at) + unit-tests + build-services matrix (8 services) + runtime-import-smoke + compose-test. The three structural lints are now **wired into CI** (v1.0.7at): `lint-redis-channels.py` runs `--strict` (enforced — a Docker-label false positive was fixed so it's clean); `lint-structure.py` + `lint-openapi.py` run permissive pending #139 (their warnings: classical-policy READMEs/tests, an adaptive-bench scenario, and the `/api/v1/metrics/*` OpenAPI entries). The unit-tests job also installs `joblib` so the anomaly IF-engine guard tests execute rather than skip.

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
| ~~Redis exporter to Prometheus~~ | ~~#116~~ | **CLOSED** v1.0.7ac | — |
| ~~AI-service `/metrics` endpoints (Prometheus format)~~ | ~~#161~~ | **CLOSED** v1.0.7al (six services + shared helper + scrape + Overview panel) | — |
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
| **Isolation Forest anomaly engine** | **Resolved (#165)** — re-fit in production-shape space v1.0.7ah (`train_production.py`, bench 25% → **91.4%**, zero under-reactions); the v1.0.7ai lb-otel-shipper IP→container-name canonicalization closed the live-stack test (**all 5 acceptance criteria met**); **v1.0.7aj flipped the compose default to `isolation_forest`** (the bonus). Calibration closed; compose default since reverted — see → | **Residual confirmed (#160 smoke):** the IF default over-excluded the single seed backend under sustained load → LB 502 on the whole pool. **v1.0.7an reverted the compose default to `threshold`** pending a fix (follow-up to #165); re-enable via `ANOMALY_ENGINE=isolation_forest`. **v1.0.7ap quorum guard (lb-sidecar `handle_anomaly` + NGINX adapter safety net) now prevents the empty-pool 502 outright — an exclusion that would drop the last active backend is refused; v1.0.7aq guards NaN/inf features that drove spurious exclusions. Re-enabling the IF default is now gated only on a live-stack smoke.** |
| **Baseline-vs-SmartLoad bench (#148)** | Only SHORT-mode runs (~2 min/side); full-length (~6 min/side) on retrained PPO owed | Multi-run batching with CIs **shipped v1.0.7am (#160)**; remaining gap is the full-length re-run after PPO retraining (§35.3 capability closed; §35.7 publishable run gated on §34.6) |
| **Anomaly + Forecast scenario walks** | Manifests + e2e tests exist; standalone `examples/scenarios/<feature>/` walk scripts do not | 1–2 hours each |

## What's WRONG (incorrect implementation)

**Nothing flagged.** Every SOT §18 claim verified by the audit matched the actual code. The closest items to "wrong" are documentation-side:

- The #155 original issue body said the BFF SSE endpoint was `/api/ui/events` — the actual endpoint is `/api/ui/engines/stream`. (Issue text was wrong; code is right; corrected in #156.)
- The `scaling_events.action` SQL column carries `"scale_out" | "scale_in"` text; v1.0.7v's new `mechanism` field rides in the envelope and textually in `reason` rather than a structured column. Defensible (no migration needed) but a future column add would let downstream consumers join on it cleanly. **Update (v1.0.7as, #141):** the `mechanism` column now exists — added to `init.sql` (fresh deployments) + migration `0001` (existing volumes). The autoscaler still also writes mechanism into `reason`; switching `SCALING_EVENT_INSERT` to populate the column is the remaining step (hygiene batch).

## What NEEDS ENHANCEMENT (works today, could be production-grade)

| Area | Enhancement | Issue |
|---|---|---|
| ~~Own-metrics~~ | ~~Prometheus `/metrics` per AI service~~ — **DONE v1.0.7al** (#161): `<svc>_up`/`_cycle_*`/`_publish_*` + decision counters across 6 services | ~~#161~~ |
| API versioning + deprecation | Formal `Sunset` / `Deprecation` header window mechanism | #134 |
| Strict lint mode | **Partial (v1.0.7at):** all three lints now run in CI (`structure-lint` job); `lint-redis-channels` is enforced (`--strict`, false positive fixed). Flipping `lint-structure` + `lint-openapi` to `--strict` still needs their warnings resolved (policy READMEs/tests, adaptive-bench scenario, OpenAPI metrics endpoints) | #139 |
| ~~DB migrations~~ | **DONE v1.0.7as (#141):** `scripts/migrate.py` numbered-SQL runner + `schema_migrations` tracking table + `infrastructure/migrations/0001_scaling_events_mechanism.sql`; `init.sql` kept in sync for fresh volumes. Remaining ops nicety: a profile-gated one-shot `migrate` compose service | ~~#141~~ |
| Request correlation ID | W3C Trace Context end-to-end propagation for per-request explainability | #143 |
| Test reorg | Migrate applicable `tests/integration/*` into `tests/e2e/<feature>/` | #140 |
| Backup / restore runbook | TimescaleDB backup story not yet documented | #142 |
| ~~Multi-run bench batching~~ | ~~Single-run point estimates today; multi-run + per-metric CIs make results publishable~~ — **DONE v1.0.7am** (#160): both harnesses take `--runs N`/`RUNS=N` (default 5) + `aggregate_runs.py` → `summary.parquet` + `mean ± CI` `SUMMARY.md` + CI-band plots (shared `_bench_common/bench_stats.py`, Student's t). Capability + smoke shipped; publishable full-length batch still gated on retrained PPO | ~~#160~~ |

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

The code is product-shippable at ~88 % for the present phase. The harder honest call lives in SOT §34 Results:

> **The harness works; the lb-sidecar mechanism works; the PPO model has not been trained for the workload the bench exposes.** v1.0.7t per-phase p95 numbers: baseline 14 / 42 / 44 / 43 ms; SmartLoad 23 / 41 / 50 / 44 ms across A_ramp / A_hold / B_anomaly / C_sustain. SmartLoad's max latency 3,082 ms vs baseline 150 ms is the lb-sidecar's NGINX-reload cost during the anomaly window.

This is the most important thing to internalize: the gap between **code shipped** and **evidence shipped** is real and larger than the % suggests. Of the three deliverables that close it, **the third is now done**:

1. **Retrained PPO** on heterogeneous-latency training distribution (Rghda's workstream — in progress on a separate machine)
2. **Full-length baseline bench rerun** after retraining (the v1.0.7r outstanding item — gated on (1))
3. ~~**Adaptive bench** to answer RQ4 quantitatively~~ — **DONE** (this session): the committed 6-min run shows the forecast-driven pool grow 1→6 and shrink back, the affirmative RQ4 result.

So the remaining evidence gap is squarely the RL/PPO story (1 + 2), which depends on the retrained model. None of these change the architecture; all close measurable, named gaps.

---

## Bottom line by lens

| If "the project" means… | Score | What gets you to 90 %+ |
|---|---|---|
| **The codebase** (services + tests + docs) | **~90 %** (was 88) | This session: #165 calibration closed (anomaly engine re-calibrated to 91.4%; compose default reverted to `threshold` v1.0.7an pending an over-exclusion fix) + the demo-ui Dev Console. Next for production maturity: #161 /metrics, #141 migrations, #139 strict lint |
| **Production-ready middleware** | **70 %** | + own-metrics + #141 migrations + #143 correlation IDs + #139 strict lint + Helm templates — 85 % |
| **Publishable evidence** | **~58 %** (was 55) | Adaptive-bench RQ4 run now committed/demonstrated; remaining is the retrained PPO + full baseline rerun + multi-run CIs (#160) — 75 % |

If you only count what is *currently shipped against the current-phase scope*, SmartLoad is a defensible product foundation. The known gaps are named, owned, and traceable to specific issues. There is no zombie surface area — every stub has either an issue number or an explicit Phase 2 deferral.

---

## Sprint state at audit time

| Sprint | Period | Phase | Open issues | Status |
|---|---|---|---|---|
| S1 | Feb 1 – Apr 24 | Phase 0 | 0 | DONE |
| S2 | Apr 28 – May 9 | Phase 1A | 0 | DONE |
| S3 | May 10 – May 23 | Phase 1B | 0 | DONE |
| S4 | May 24 – Jun 6 | Phase 2 | **4** | Carry-forward Rghda + Nada workstreams (#98, #99, #104, #118; #101 closed v1.0.7ab, #7 closed-with-rationale 2026-06-11 — SMD acquired + NAB rejected as unfit per PR #158, datasets gitignored by design rather than committed) |
| S5 | Jun 7 – Jun 20 | Phase 3A | **0** | **DONE** — #117 closed v1.0.7ad (acceptance-test pattern + tests/README.md + PR template); #116 v1.0.7ac; #101 v1.0.7ab; #103 v1.0.7aa; #159 / #163 / #164 mid-sprint |
| S6 | Jun 21 – Jun 30 | Phase 3B (impl) | **24** | Implementation & release hardening — feature delivery (#130 webhooks, #131 OUI.8, #133 Helm, #124/#125 OUI.6/.7, #56 auth model), production maturity (#139 strict lint, #140 test reorg, #141 migrations, #143 correlation IDs, #161 /metrics, #134 versioning + deprecation, #142 backup runbook), integration adoptions (#145/#146/#147/#150), regression + release (#37, #42, #43, #46, #126), final bench + multi-run CIs (#39, #160) |
| S7 | TBD (follows S6) | Phase 3C (docs) | **7** | Final report & presentation — pure prose deliverables (#16, #21, #40, #44, #45, #162) + demo script & slides (#41). Split out from legacy S6 on 2026-06-11 so 0% on S7 with S6 done = code complete, writeup remaining |

**Phase 2 — SaaS adaptation** (no sprint, explicit deferral per SOT §25): **3 open** — #129 multi-tenancy, #132 tenant API keys + RBAC, #135 rate limiting. Not counted against present-phase completeness; these are scope decisions, not gaps. Milestone created 2026-06-11 to make the deferral explicit (was previously implicit via "no milestone").

Total open issues at 2026-06-11 refresh: **43** (6 S4 + 3 S5 + 24 S6 + 7 S7 + 3 Phase 2). The 2026-06-11 retriage moved 16 previously unmilestoned issues into buckets (13 → S6, 3 → new Phase 2 milestone) and split the legacy "Sprint 6 — Final Report & Presentation" into S6 (implementation & release hardening) + S7 (docs-only — 7 prose deliverables peeled off from the old S6) so docs progress no longer masks impl completion.

**2026-06-13 session issue movement:** **#165 closed** (the unmilestoned Isolation Forest production-scale calibration — all 5 acceptance criteria + bonus met across v1.0.7ah–ak); a follow-up to fix the over-exclusion that forced the v1.0.7an compose-default revert to `threshold` remains open. The demo-ui Dev Console redesign (v1.0.7af/ag) was untracked tooling improvement — `tools/demo-ui/` is a dev artefact, exempt from the structural lint's per-feature triad, so it carries no issue. Two new follow-ups worth filing: the AI-service own-metrics gap already tracked as #161, and a small test-harness note (the live-stack test is now RL-policy-independent; v1.0.7ak). Net open issues unchanged otherwise.

---

## How to refresh this doc

Re-run the audit when:

- A major release lands (e.g. #156 R2 closes — re-audit infra layer)
- A new architectural layer is added
- A sprint boundary passes
- Sign-off / external review is needed

The methodology is reproducible: four parallel audits (decision plane / data plane / control plane / infra) read SOT §18 Build Status claims and verify each against the actual code, then merge their reports into this document. The most recent baseline commit at the top of this file is the canonical anchor.
