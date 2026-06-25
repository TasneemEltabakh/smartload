# SmartLoad Enhancement Roadmap

Status: planning. Last updated 2026-06-25.

This roadmap exists to drive two outcomes in parallel and with equal weight:

1. **Publishable paper.** A defence-ready research result whose claims survive
   adversarial review. The honest spine today is strong (anomaly-driven exclusion
   decisively beats round-robin at equal capacity, closed-loop proactive
   autoscaling is demonstrated, and every learned engine degrades to a
   deterministic baseline). The work below closes the caveats that currently
   weaken it.
2. **Productisable system.** A deployable, reliable, extensible control plane
   that a sponsor or integrator can stand up, trust, and see differentiated value
   from inside a short demo.

The two goals are not competing backlogs. Several items move both at once. The
plan leads with those dual-use items, then splits the remainder into a paper wing
and a product wing.

This is a forward-looking planning document. It does not change the description of
the system as it ships today, so the three canonical documents (SOURCE_OF_TRUTH,
PROJECT_WALKTHROUGH, README) are not edited by this file. Each work item below
names the canonical documents it will require updating when it lands.

---

## 1. Where the two goals stand today

### Paper readiness

The paper's defensible contributions as built:

- Anomaly-driven exclusion beats round-robin decisively at equal capacity
  (roughly 8x to 250x tail-latency reduction across the degrade and slow
  scenarios), robust across three runs.
- Closed-loop proactive autoscaling closes on the live stack: the pool grows
  toward budget on a forecast burst and shrinks on demand recession.
- A deterministic-fallback safety property: every learned component has an
  artefact-free baseline it degrades to, so a bad model cannot raise an outage.

What a reviewer will press on, and which this roadmap fixes:

- Routing is a null result. The benchmark runs on an equal-capacity pool where an
  even split is provably optimal, so no router can beat round-robin. The learned
  router has no opportunity to show value.
- The closed-loop autoscaling result rests on a forecast signal that is
  effectively load-decoupled (it holds near a constant rate regardless of demand).
  The controller is working around a signal it cannot fully trust.
- There is no comparison against a recognised external baseline. The first
  question at review will be "versus Kubernetes HPA?".
- Statistical rigour is partial: multi-run confidence intervals exist on the
  volatile phases but not on every phase.

### Product readiness

Code completeness is high (~88 percent for the current phase). What is missing for
a credible product and sponsor demo:

- A known, reproducible empty-pool 502 failure loop exists with no regression
  test guarding it.
- One-command deployment (Helm or K8s manifests) does not exist; only single-host
  Docker Compose.
- Only one real load-balancer adapter exists (NGINX). ALB, Envoy, and HAProxy are
  stubs, so the extensibility claim is unproven.
- No multi-tenancy, API keys, RBAC, operator-UI authentication, webhooks, or rate
  limiting. These are the questions a commercial sponsor asks first.
- Metrics still live in Grafana, requiring a context switch out of the operator UI
  during a demo.

---

## 2. Tier 0: dual-use core (do these first)

Each item here advances the paper and the product at the same time. This is the
highest-leverage work and should be sequenced first.

### 0.1 Eliminate the pool-collapse 502 loop, with a regression test
- **Status:** DONE (2026-06-25). Both code fixes were already in the tree and
  wired (detector allowlist + sidecar live-pool guard); the remaining gap was a
  test proving they compose to break the loop. Closed by
  `tests/unit/pool-collapse/test_pool_collapse_loop.py` (composed-unit regression
  across the real shipper, detector, and sidecar functions). See the resolution
  note in the audit finding.
- **Serves:** both.
- **Source:** `audit/_findings/anomaly-pool-collapse-rootcause.md`.
- **Problem:** when the backend pool empties under load, NGINX emits the upstream
  block name (`backend_pool`) as a phantom upstream. The shipper passes it through,
  the anomaly detector scores it as a real backend, the sidecar tries to exclude
  it, and the pool empties further. The loop is self-sustaining and needs a manual
  `docker compose restart` to clear.
- **Fix:** allowlist scoring in the anomaly detector so it only scores known
  backends, plus a sidecar reject for any verdict whose key is not a discovered
  backend. Add a cold-start confirmation window as defence in depth.
- **Acceptance:** an integration test that drives the pool empty under load and
  asserts the system recovers on its own once load eases, with no phantom-backend
  exclusion recorded.
- **Why first:** smallest fix with the largest reliability payoff. Removes a live
  outage for the product and removes a failure mode that undercuts the
  bounded-failure-modes claim in the paper.
- **Docs to sync on landing:** SOURCE_OF_TRUTH (anomaly precedence and quorum
  guard), `docs/planned/anomaly-detection-known-weaknesses.md`.

### 0.2 Heterogeneous-capacity benchmark, and resolve PPO
- **Serves:** both.
- **Source:** issues #190 (bench), #188 (PPO train or retire).
- **Problem:** every routing benchmark is equal-capacity, the one setting where
  round-robin is optimal by construction. The learned router cannot earn its keep
  and PPO sits dormant in shadow.
- **Work:**
  1. Build an unequal-capacity testbed where backend identity correlates with
     latency or throughput, so routing decisions carry a discriminating reward.
  2. Re-run the routing comparison (monotone, PPO, round-robin, p2c, JSQ, LRT) on
     it with multi-run confidence intervals.
  3. Either retrain PPO on the heterogeneous traces so it has a signal to learn
     from, or formally retire it and document the null result cleanly.
- **Acceptance:** a committed benchmark report showing how each router performs on
  a non-uniform pool, with a clear verdict on learned routing.
- **Why central:** this is the single experiment that converts the routing null
  result into either a positive result or an honest, defensible characterisation.
  For the product it is the difference between "intelligent routing" and "NGINX
  with health checks".
- **Docs to sync on landing:** SOURCE_OF_TRUTH (routing verdict), thesis chapters
  03b/04b/05/06, `services/rl-engine/README.md`.

### 0.3 Helm chart, then the Kubernetes HPA comparison
- **Serves:** both.
- **Source:** issue #133 (Helm), plus a new comparison experiment.
- **Problem:** no one-command deploy exists, and the paper has no recognised
  external baseline.
- **Work:**
  1. Fill `infrastructure/helm/smartload/templates/` so one `helm install` brings
     up the full stack (eight services, Redis, TimescaleDB, config, secrets,
     volumes, ingress).
  2. Stand the stack up on Kubernetes and run the adaptive-scaling benchmark with
     Kubernetes HPA as the baseline against SmartLoad's forecast-driven autoscaler.
- **Acceptance:** a reproducible `helm install` and a committed report comparing
  SmartLoad autoscaling against HPA on the same workload.
- **Why central:** this is the strongest bridge between the two goals. The deploy
  automation is exactly what a sponsor expects, and the HPA comparison is what
  makes the autoscaling result publishable rather than "it works on our stack".
- **Docs to sync on landing:** README (deploy section), SOURCE_OF_TRUTH
  (deployment topology), `infrastructure/helm/README.md`.

### 0.4 Couple the forecast to load, and consolidate the anti-flap autoscaler
- **Serves:** both.
- **Source:** issues #189 (forecast coupling), #183 (autoscaler consolidation).
- **Problem:** the deployed forecaster holds near a constant rate regardless of
  demand, which is the upstream cause of the autoscaler flap. Two controllers
  exist; the deployed one flaps and the safer hysteresis controller is inert.
- **Work:**
  1. Blend the harmonic-residual forecast with live offered rate, or add
     load-coupled features, so the forecast tracks demand.
  2. Deploy the hysteresis controller with scale-in confirmation, retire the
     flapping one, and reconcile state authority so exclusion does not desync on
     restart.
- **Acceptance:** the forecast tracks offered load on the bench, and the pool no
  longer flaps without the bound being pinned.
- **Why central:** makes the closed-loop autoscaling claim clean for the paper and
  makes the product behave correctly under real demand swings.
- **Docs to sync on landing:** SOURCE_OF_TRUTH (forecast and autoscaler sections),
  thesis chapters 04b/05/06, `docs/modules/forecasting.md`,
  `docs/modules/autoscaler.md`.

---

## 3. Tier 1: paper wing

Sequenced after the Tier 0 core. Primary purpose is research rigour, though most
items also harden the product.

### 1.1 Coupled control-loop integration test (#182)
Assert the cascade, quorum, and recovery invariants of the
anomaly to reroute to scale chain under sustained load. Doubles as reliability
evidence for the product.

### 1.2 Statistical-rigour pass
Extend multi-run confidence-interval reporting to every benchmark phase, not only
the volatile ones. Re-run the canonical benches under the retrained model so the
headline numbers come from full-length runs.

### 1.3 Reconcile chapters with results (#195)
Audit every claim in the thesis prose against the committed data. Remove or hedge
any "beats round-robin" phrasing where the data shows a tie. Fill any remaining
benchmark macros from committed reports.

### 1.4 First-class data-plane health surface
Several `/health` endpoints report control-plane liveness and can return 200 while
the data plane is unservable, the exact gap that masked the original 502. Add a
real data-plane probe and replace the proxy-arrivals signal with a true arrivals
feed. Strengthens the bounded-failure-modes section and the product's reliability.

---

## 4. Tier 2: product wing

Sequenced in parallel with the paper wing once the Tier 0 core is in. Primary
purpose is sponsor appeal and commercial credibility.

### 2.1 A second real LoadBalancerAdapter: HAProxy (#147)
HAProxy is the simplest of the three stubs. A second working adapter proves the
abstraction is not NGINX-shaped fiction, which is both a product extensibility
story and a generality claim the paper can make.

### 2.2 Embedded metrics dashboards in the operator UI (#131)
Surface the core metrics inside the operator UI so a demo never has to leave for
Grafana. This is the single biggest "looks like a product" improvement.

### 2.3 Multi-tenancy backbone (#129, #132, #56, #125)
Phased: `tenant_id` across DB, Redis namespaces, and policy storage; then
tenant-scoped API keys and RBAC; then operator-UI authentication. This is the SaaS
credibility a sponsor asks about first.

### 2.4 Webhook dispatcher (#130) and rate limiting / quotas (#135)
Outbound HMAC-signed events with retries (the service directory is already
scaffolded), plus per-token rate limiting and quotas on the public API. Pair with
API versioning and deprecation policy (#134).

---

## 5. Tier 3: hardening (supports both, lower urgency)

- Model-ops: content hashing, manifest pinning, version-skew checking, and an
  automated drift-detection and retraining pipeline. Today promotion is manual.
- Configuration immutability: treat `policy.yaml` as append-only so the
  complete-audit claim holds even when the file is edited out of band.
- Correlation-ID threading onto Redis envelopes (the HTTP surface is done).
- TimescaleDB backup and restore runbook plus scripts (#142).
- Dataset documentation and licensing appendix (#45).

---

## 6. Sequencing

**Phase 1, foundation (Tier 0).** Land 0.1 first. Run 0.2, 0.3, and 0.4 in
parallel where capacity allows; PPO retraining (part of 0.2) is already a separate
workstream on dedicated hardware. At the end of Phase 1 the paper is defensible
(routing characterised, forecast coupled, HPA baseline in hand) and the product is
deployable, reliable, and differentiated.

**Phase 2, split wings.** Run the paper wing (Tier 1) and the product wing
(Tier 2) in parallel. The paper wing finalises rigour and the write-up. The
product wing builds the sponsor demo (second adapter, embedded dashboards, first
slice of multi-tenancy).

**Phase 3, hardening (Tier 3).** Fold in as capacity allows, prioritising
model-ops and config immutability since both appear in the thesis limitations.

The efficiency of this plan comes from Tier 0: items 0.2, 0.3, and 0.4 are
simultaneously the paper's weakest caveats and the product's missing
differentiators, so the foundation work is never a trade of paper-time against
product-time.

---

## 7. Traceability

| ID  | Item                                   | Goal     | Issue(s)             | Effort |
|-----|----------------------------------------|----------|----------------------|--------|
| 0.1 | Pool-collapse 502 fix + test           | Both     | audit finding        | S      |
| 0.2 | Heterogeneous bench + PPO resolution   | Both     | #190, #188           | L      |
| 0.3 | Helm chart + K8s HPA comparison        | Both     | #133                 | M + M  |
| 0.4 | Forecast coupling + anti-flap          | Both     | #189, #183           | M      |
| 1.1 | Coupled control-loop integration test  | Paper    | #182                 | M      |
| 1.2 | Statistical-rigour pass                | Paper    | #39, #43             | M      |
| 1.3 | Reconcile chapters with results        | Paper    | #195                 | S      |
| 1.4 | Data-plane health surface              | Both     | (thesis limitation)  | M      |
| 2.1 | HAProxy adapter                        | Both     | #147                 | M      |
| 2.2 | Embedded UI dashboards                 | Product  | #131                 | M      |
| 2.3 | Multi-tenancy + keys + RBAC + auth     | Product  | #129, #132, #56, #125| L      |
| 2.4 | Webhooks + rate limiting + versioning  | Product  | #130, #135, #134     | M      |
| 3.x | Hardening bundle                       | Both     | #142, #45            | M      |

Effort key: S is roughly a day, M is a few days, L is a week or more.
