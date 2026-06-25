# Heterogeneous-Capacity Routing Benchmark — Design

Status: scoped, not yet implemented. Owner: TBD. Tracks issue #190 (bench) and
feeds #188 (PPO: deploy or retire).

This document scopes the benchmark before any harness code is written. It defines
the claim under test, how the heterogeneity is created, what is compared, the
metrics and decision rule, the run protocol, the host the run needs, the validity
threats, and what result closes the issue. It deliberately reuses the existing
benchmark machinery rather than building a parallel one.

---

## 1. Why this benchmark exists

SmartLoad already has two levels of routing evidence. This benchmark adds the
third, which is the only one that closes the external-validity gap the thesis
names.

| Level | Harness | What it proves | Limitation |
|-------|---------|----------------|------------|
| Simulator | `experiments/rl-routing-bench/` (closed-loop M/G/c sim) | Router ranking across homogeneous / heterogeneous / degrading scenarios, with the monotonicity probe | Synthetic queueing model, not the real data plane |
| Live-backend cross-check | `experiments/rl-routing-bench/live_stack.py` | Sim ranking holds on the real Node backend over real HTTP | The harness applies policy weights in-process; it bypasses NGINX, the real rl-engine, and the lb-sidecar rewrite path |
| **Full live stack (this doc)** | new, reusing `adaptive-bench/run.py` | The router earns its keep end to end: real NGINX data plane, capacity-tiered backends, the real rl-engine to lb-sidecar to NGINX path, live telemetry and anomaly loop | The honest, citable test |

The thesis records the gap directly: the canonical live bench is equal-capacity
(where an even split is provably optimal, so no router can beat round-robin), and
the heterogeneous result lives only in the simulator. On an equal-capacity pool a
capacity-aware router has nothing to exploit. This benchmark builds the unequal
pool on the live stack so the router has a real opportunity, and measures whether
it takes it.

Two outcomes both have value:
- The capacity-aware router (monotone, and possibly a retrained PPO) measurably
  beats round-robin and the classical baselines on the live heterogeneous pool.
  This converts the routing null result into a positive result and gives the
  product its "intelligent routing" differentiator.
- It does not. Then the honest characterisation ("routing parity even under
  heterogeneity, the win is in exclusion and autoscaling") is made on the real
  stack instead of inferred from a simulator, which is itself a defensible
  contribution and settles #188 by retiring PPO cleanly.

---

## 2. Claim under test

> On a live, unequal-capacity backend pool under overload, a capacity-aware
> router (monotone; and a heterogeneous-trained PPO if available) reduces
> SLA-violation rate and served tail latency relative to NGINX round-robin and to
> the classical baselines, without violating latency-monotonicity.

The null hypothesis is parity with round-robin. The benchmark must be able to
reject it, which is precisely what an equal-capacity pool cannot do.

---

## 3. Independent variable: how the heterogeneity is created

Heterogeneity must be **real capacity difference**, not modeled latency. A backend
made slow with a fixed added delay (`SLOW_DELAY_MS`) still has unlimited
throughput, so it never builds a queue and a capacity-aware router has little to
balance. The honest unequal-capacity regime is built from genuine per-backend
throughput limits, which the existing `test-backend` already supports:

- `CPU_BOUND=true` — the backend burns real CPU for its service time instead of
  sleeping, so a CPU limit actually throttles its throughput (see
  `test-backends/app.js`).
- Docker `cpus:` limit per replica — the capacity cap. This is the knob that
  creates the tiers. (Not currently set in `docker-compose.yml`; the benchmark
  adds a compose override / profile that pins it per replica.)
- `WORKERS` and `queueMax` — concurrency slots and bounded FIFO, already wired,
  set per tier so the queue knee sits where the sim's profiles put it.

**Default capacity tiers (5 backends).** Mirror the ratios of the simulator's
heterogeneous `BackendProfile` set so the live result is comparable to the sim:

| Replica | Tier | `cpus:` | Intent |
|---------|------|---------|--------|
| backend-1 | large | 1.00 | high-capacity |
| backend-2 | large | 1.00 | high-capacity |
| backend-3 | medium | 0.50 | mid |
| backend-4 | medium | 0.50 | mid |
| backend-5 | small | 0.25 | the one round-robin overloads first |

Total pool capacity is deliberately skewed so an equal split (round-robin)
saturates the small replica well before the large ones, which is the condition a
capacity-aware router should exploit. The exact ratios are a knob; the design
requirement is "non-uniform, with at least a 4:1 spread top to bottom, mirroring
the sim profile."

A modeled-latency variant (`SLOW_DELAY_MS` tiers, no CPU cap) is kept as a
secondary, clearly-labelled scenario only, to show the contrast; it is not the
headline.

---

## 4. Contenders (routers compared)

Reuse the policy set already in `experiments/rl-routing-bench/contenders.py` and
`baselines_ext.py`, driven on the live stack via `RL_POLICY`:

- `round_robin` — the null baseline (NGINX-native and via the engine).
- `monotone` (`candidate_mono`) — the recommended capacity-aware router.
- `least_connections`.
- `p2c` (power-of-two-choices) and `jsq` (join-shortest-queue) — the strong
  classical baselines the sim already compares against.
- `ppo` — only if a heterogeneous-trained artifact exists (the #188 dependency).
  If not available at run time, PPO is reported as "not deployed" and #188 is
  settled by that absence, not skipped silently.

Every learned/heuristic contender must also pass the latency-monotonicity probe
(`experiments/rl-routing-bench/monotonicity_probe.py`) as a gate; a router that
wins by anti-monotone sacrificial shedding is reported but not recommended (the
sim REPORT documents this trap on the served-p95 metric).

---

## 5. Workload profiles

Drive load with Locust from a separate node (see Section 7). Profiles:

1. **Steady overload** — constant arrival rate set so the pool runs at ~110-130%
   of round-robin-effective capacity (round-robin overloads the small tier, the
   capacity-aware router should not). The primary scenario.
2. **Spike** — a step from moderate to high arrival rate, to test transient
   behaviour and the anti-concentration rail under surge.
3. **(Secondary) modeled-latency heterogeneity** — the `SLOW_DELAY_MS` contrast
   scenario from Section 3.

Anomaly injection and autoscaling are held OFF for the routing-isolation runs (we
are measuring routing, not exclusion or scaling), then a combined run is added at
the end to show the loops compose. Pin the pool size so the autoscaler cannot
change membership mid-run during the routing-isolation phase.

---

## 6. Metrics and decision rule

Primary, per contender, with multi-run mean and 95% CI (via
`experiments/_bench_common/bench_stats.py`):

- **SLA-violation %** (fraction of requests over the 200 ms SLO) — the headline.
- **Served p95 / p99 latency**.
- **Error rate** (503/502 shed) — reported alongside latency to catch the
  sacrificial-shedding trap: a policy that drops the served-p95 by forcing 503s on
  the small backend is not winning. Served latency and error rate must be read
  together.
- **Throughput / goodput** (2xx per second).
- **Load distribution / fairness** across tiers (so we can show the
  capacity-aware router actually shifted load off the small backend).

Decision rule (multi-dimensional, matching the thesis's existing convention):
a contender "beats round-robin" if it improves SLA-violation % with
non-overlapping CI **and** does not regress goodput or error rate. Latency-tail
improvement is supporting evidence. The monotonicity probe is a hard gate for the
"recommended" label.

---

## 7. Environment and host sizing

The measured quantity is per-backend capacity, so the host must not let anything
else steal that capacity. Two requirements dominate:

1. **Isolate the load generator from the system under test.** If Locust shares
   cores with the backends it consumes the capacity being measured. Run it on a
   separate node.
2. **Enough dedicated cores that the host is comfortably under ~60% utilisation
   at peak**, or the capacity tiers blur into contention noise.

**Local dev machine (Intel i7-10750H, 6 physical / 12 logical cores, 16 GB):**
sufficient for methodology validation and pipeline smoke only. The full stack is a
dozen-plus containers; adding five CPU-limited backends plus Locust saturates six
physical cores and contaminates the capacity signal. Do not produce the committed
result here.

**Recommended for the committed run: a two-node setup**, same region / low-latency
network so client-measured latency reflects server behaviour, not WAN jitter:
- **SUT node** — ~8 vCPU / 16 GB. Backend CPU caps sum to ~3 vCPU (1+1+0.5+0.5+0.25),
  leaving headroom for NGINX, the control plane, TimescaleDB, Redis, and the OTel
  collector. Pin backend `cpus:` so capacity is the controlled variable.
- **Load node** — 2-4 vCPU running Locust only.

This is the same cloud footprint the Helm / K8s-HPA comparison (roadmap item 0.3)
needs, so stand both up together and run both results in one consistent, citable
environment.

---

## 8. Run protocol

Follow the `adaptive-bench/run.py` batch pattern (it already does live-stack
multi-run orchestration with per-run MANIFEST capture, collectors, and CI
aggregation):

- **Warmup** discarded (let queues and EWMA windows settle).
- **Measurement window** long enough for stable p99 (the thesis flags short
  two-minute profiles as noisy; target a longer window than the canonical bench).
- **Repetitions**: at least 5 independently-seeded runs per contender so the CI is
  meaningful; report mean ± 95% CI on every metric, every phase (closing the
  thesis's partial-CI gap at the same time).
- **MANIFEST per run** capturing every knob (capacity tiers, `RL_POLICY`, arrival
  rate, seed, image digests, env) so a run is reproducible to the digit.
- **Restore** policy and env in a `finally:` block.

---

## 9. Validity threats and controls

- **Load generator contention** — isolate on a separate node (Section 7).
- **Sacrificial shedding on the served-p95 metric** — report served latency with
  error rate and goodput; the monotonicity probe gates the recommendation.
- **Background load** — both sides see the same dev/control-plane load; capture it
  in MANIFEST; prefer a clean SUT node.
- **Capacity drift** — pin `cpus:` and pin pool membership (autoscaler off) during
  routing-isolation runs.
- **Single-run variability** — minimum five seeded runs with CIs (the thesis notes
  one run once misled entirely).
- **Sim-to-live comparability** — mirror the simulator's heterogeneous profile
  ratios so the live result can be read against the sim ranking.

---

## 10. Reuse map (do not rebuild)

| Need | Reuse |
|------|-------|
| Live multi-run orchestration, collectors, MANIFEST, CI aggregation | `experiments/adaptive-bench/run.py` (adapt: routing scenario instead of scaling) |
| Router set + factories | `experiments/rl-routing-bench/contenders.py`, `baselines_ext.py` |
| Monotonicity gate | `experiments/rl-routing-bench/monotonicity_probe.py` |
| CI maths | `experiments/_bench_common/bench_stats.py` |
| Capacity tiers | `test-backends` knobs (`CPU_BOUND`, `WORKERS`, `queueMax`) + a compose override pinning `cpus:` |
| Load shapes | `experiments/adaptive-bench/locust/` (new routing-overload shape) |

New code is therefore small: a compose override / profile for the capacity tiers,
a routing-scenario Locust shape, and a thin orchestrator that sets `RL_POLICY` per
contender and calls the existing run/aggregate path.

---

## 11. Acceptance criteria

Closes #190 when:
- A committed batch (>= 5 seeded runs per contender) on the live stack with the
  capacity tiers of Section 3, reporting all Section 6 metrics with 95% CIs.
- A SUMMARY.md with the per-contender table and the explicit verdict on the
  Section 2 claim (beats round-robin / parity / loses), read with the
  served-vs-error caveat.
- The monotonicity probe result recorded for every learned contender.

Feeds #188: the PPO column is either a real heterogeneous-trained result or an
explicit "not deployed / retired" line. Either settles the PPO verdict on the live
stack rather than the simulator.

---

## 12. Deliverables

- `experiments/heterogeneous-capacity-bench/` — compose override, Locust shape,
  orchestrator, README.
- `experiments/heterogeneous-capacity-bench/results/<timestamp>/` — per-run data,
  MANIFESTs, `SUMMARY.md`.
- Docs to sync on landing: SOURCE_OF_TRUTH (routing verdict), thesis chapters
  03b/04b/05/06, `services/rl-engine/README.md`, and the roadmap item 0.2 status.

---

## 14. Implementation wiring notes (from the live stack)

Captured during scoping so the harness build does not rediscover them.

- **`test-backend` is a single scaled service** (`docker-compose.yml`,
  `deploy: replicas: 5`); all five replicas share one env block, so a per-replica
  `cpus:` cap cannot be set on it. Real capacity tiers require a benchmark compose
  override that splits it into five named services (`test-backend-1..5`), each
  with its own `cpus:` limit, `CPU_BOUND=true`, and a network alias preserving the
  `smartload-test-backend-N` DNS name that NGINX, `ALL_BACKENDS`, and
  `policy.yaml`'s `max_backends` already expect. The override must also neutralise
  the base scaled service and the load-balancer's `depends_on:
  service_healthy: test-backend` wait. This is the main new infra and is best
  iterated on a real Docker host.
- **`/_admin/delay` is modeled latency only** (`test-backends/app.js`) — it adds
  service time but does not cap throughput, so it is the secondary contrast
  scenario, not the headline capacity mechanism. `/_admin/stats` exposes
  workers/queue for observation. There is no runtime knob to change a replica's
  workers or CPU, which is why capacity tiers are a compose-time concern.
- **Driving live (non-shadow) routing** requires: `RL_MODE=active` (compose
  default is `shadow`), a policy override setting `rl_confidence_threshold=0` (the
  shipped `policy.yaml` is `0.6`, which would reject sub-0.6 recommendations),
  `safe_mode=false`, and `LB_SIDECAR_RUNLOOP_ENABLED=true` (already the default).
  The round-robin baseline can be run either as NGINX-native (sidecar equal
  weights, `RL_MODE=shadow`) or `RL_POLICY=round_robin` active; report which.
- **Routing isolation**: hold the anomaly detector and autoscaler off (or pin pool
  membership) during the routing-only runs so exclusion and scaling do not
  confound the routing metric; add one combined run at the end to show the loops
  compose.

## 13. Open decisions (resolve before implementing)

1. Final capacity-tier ratios (default in Section 3, pending a smoke run that
   confirms round-robin actually overloads the small tier at the chosen arrival
   rate).
2. Whether a heterogeneous-trained PPO artifact will exist by run time (#188); if
   not, the bench still runs and reports PPO as not deployed.
3. The exact cloud host (single-region two-node), ideally shared with the
   Helm / K8s-HPA work (item 0.3).
4. Measurement window length and arrival rate, set from the smoke run.
