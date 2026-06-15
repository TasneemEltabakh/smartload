# SmartLoad — Thesis-Strengthening Roadmap

**Scope:** the decision-plane services (`anomaly-detector`, `lb-sidecar`, `autoscaler`,
`forecasting`, `rl-engine`), the shared contracts/adapters, and the **benchmarks**.
The operator/demo UIs, Grafana, and other observability services are **out of scope**
(not thesis-load-bearing — there is no UI work here).

**How to read this:** every item is tagged with **Effort** (S = hours, M = a day or
two, L = a week+), **Risk** (Low / Med / High = blast radius + chance of regressing a
working result), and **Thesis value** (Low / Med / High = how much it strengthens the
defensible claim). Items are grouped into **risk tiers, safest first**. Pick a *track*
(§9) and pull items in order; nothing in a higher tier should be started before its
listed dependencies in a lower tier are stable.

> The single most important decision is in §1. Read it first — it changes which
> half of this document you execute.

---

## 1. The central decision: what does the evidence let you claim?

Everything found during the audit and the spike-recovery work points one direction:
**SmartLoad's measurable advantage is anomaly-driven *exclusion* and *capacity holding/
scaling* — not fine-grained RL *routing*.** On a homogeneous backend pool the routing
weights add no benefit and actively cause harm (concentration → queue overflow →
self-benching cascade). The wins are:

- **Hidden bad backend** (a slow/shedding backend NGINX `max_fails=0` can never eject):
  SmartLoad excludes it → ~15× fewer errors, ~60× better tail.
- **Uniform spike**: hold the pool even, don't out-think the surge → recovered from a
  loss to a decisive win once the routing skew was bounded and benching suppressed.

Two ways to build the thesis on this:

| | **Track A — Conservative (recommended)** | **Track B — Ambitious** |
|---|---|---|
| Claim | "Anomaly-driven exclusion + bounded routing + autoscaling beats static RR; fine-grained RL routing is *unhelpful-to-harmful* on homogeneous pools and must be bounded under overload." | "Learned routing improves tail latency on *heterogeneous* pools, on top of A." |
| Needs | Tiers 0–2 + ablation/stats benchmarks | All of A **plus** a heterogeneous testbed, a working forecast, and a trained+deployed PPO |
| Risk to "done" | Low — the evidence already exists | High — new ML + new testbed, uncertain payoff |
| Examiner exposure | Low — you *own* the negative result | Medium — must defend the learned policy beats the heuristic |

**Recommendation:** build the spine as **Track A**, present the routing limits as a
*finding* (a contribution, not a failure), and treat Track B as an optional stretch
chapter only if time allows. The rest of this document is ordered so Track A falls out
of Tiers 0–2 and Track B out of Tiers 3–4.

---

## 2. Tier 0 — Already landed this session (baseline; document & commit)

These are implemented, unit-tested (310 unit tests green), and validated in the
isolated diagnostic (uniform 110-concurrent overload: **57% → 1.3% errors**, pool held
at 5, zero benching). Pending the RUNS=3 benchmark + commit.

| ID | Change | Where | Effect |
|---|---|---|---|
| T0.1 | `clamp_weight_skew` — bound routing skew across the merged pool | `lb-sidecar/runloop.py` | kills weight concentration **and** partial-ranking starvation |
| T0.2 | Skew fraction `0.75` (≤1.33:1), provably **below** the detector's outlier margin | `lb-sidecar/runloop.py` | routing can no longer manufacture an outlier its own detector benches |
| T0.3 | `#3` absolute pool-overload guard (median past `error_rate_threshold` / `slo_p95_latency_ms` ⇒ suppress all exclusions) | `anomaly-detector/runloop.py` | no benching cascade on a *plateaued* surge (where the `#2` ramp test goes quiet) |
| T0.4 | Detector **exclusion-clock hydration** from `backend_health` on startup | `anomaly-detector/app.py` | a backend left `down` across a process restart can recover (no-recovery-across-restart deadlock) |
| T0.5 | Sidecar **health reconciliation** — clear a stale conf-inherited `down` when `backend_health` says healthy | `lb-sidecar/app.py` | `backend_health` (not the stale conf) is the restart source of truth |
| T0.6 | Benchmark: `MIN_BACKENDS=5` pin (true equal-capacity) + per-side **routing reset** | `experiments/adaptive-advantage/run.sh` | removes the autoscaler-flap and stale-`down` confounds from the 5v5 A/B |

**Action:** once the RUNS=3 batch confirms a stable low C_spike across all three runs,
commit the stack and update `experiments/adaptive-advantage/README.md` with per-run
(not just averaged) numbers and the before/after.

---

## 3. Tier 1 — Safest, highest thesis-value-per-risk (purely additive)

No production behavior changes. All tests/benchmarks/docs. **Do these next regardless
of track** — they are what turns "I fixed it" into "here is rigorous evidence."

### T1.1 — Ablation benchmark *(Effort M · Risk Low · Thesis HIGH)*
Run `adaptive-advantage` with each fix toggled off, one at a time, to quantify its
contribution: baseline-RR, +clamp only, +`#3` only, +pin only, +reset only, all-on.
Gate each fix behind an env flag (`CLAMP_MIN_FRACTION=0` disables the clamp;
`ANOMALY_SLO...`/`overload_*` knobs disable `#3`; `MIN_BACKENDS=1` unpins). Produces a
contribution table — *thesis gold*: it converts a pile of fixes into a measured
decomposition of the result.
- Deps: T0.* committed.

### T1.2 — Statistical rigor in `compare.py` *(Effort S · Risk Low · Thesis HIGH)*
Report **mean ± stdev / 95% CI** per phase across N runs, not a single number. The
run-1-good / run-2-collapsed episode is the reason — one run proves nothing. Add a
"runs" column and flag phases whose CI crosses the baseline (not significant).

### T1.3 — Coupled-loop integration test *(Effort M · Risk Low · Thesis HIGH)*
Every real failure was *emergent from coupling* — unit tests structurally cannot catch
them. Add a compose-based test (its own tiny harness, or pytest + docker) that drives
the spike and asserts invariants: **no cascade, pool ≥ quorum, no healthy backend stuck
`down`, errors < threshold**. This is the single most valuable test artifact in the
project and doubles as reproducibility evidence.

### T1.4 — Regression tests pinning each fixed defect *(Effort S · Risk Low · Thesis Med)*
One focused test per defect so it cannot silently return: D1 flap, no-recovery trap,
over-exclusion cascade, spike concentration, routing-induced benching, restart
deadlock. Mostly unit-level (the suppressor / clamp / hydration already have hooks).

### T1.5 — Unit-test the new hydration functions *(Effort S · Risk Low · Thesis Low)*
`_hydrate_exclusion_clocks` (detector) and the `_hydrate_excluded_from_db` reconcile
branch (sidecar) are currently untested — they touch the DB so they need a mocked
cursor. Add focused tests.

### T1.6 — Fix the `test_runloop.py` basename collision *(Effort S · Risk Low · Thesis Low)*
`tests/unit/anomaly-detector/test_runloop.py` and `tests/unit/lb-sidecar/test_runloop.py`
can't be collected together (pytest module-name clash). Rename one (e.g.
`test_anomaly_runloop.py`) or add `__init__.py`/`conftest` package markers so the full
suite runs in one invocation (CI hygiene + an examiner running `pytest` once).

### T1.7 — Methodology + canonical benchmark write-up *(Effort M · Risk Low · Thesis HIGH)*
Document: closed- vs open-loop, the queue-knee math (`QUEUE_MAX`, `WORKERS`,
`max_fails=0`), why the 5-phase shape, what each phase isolates. Mark
`experiments/baseline-vs-smartload` **superseded** (the 50-user closed-loop tie is a
benchmark artifact — say so explicitly) and make `adaptive-advantage` the single
canonical benchmark. A clear methods chapter is half of a benchmarking thesis.

### T1.8 — "Limitations & Future Work" from the audit *(Effort S · Risk Low · Thesis Med)*
Lift the audit's confirmed-defect list and this roadmap's Tier 3–4 into an honest
limitations section. Owning the forecast/RL weaknesses pre-empts the examiner.

---

## 4. Tier 2 — Localized fixes & refactors (well-understood, bounded blast radius)

Behavior changes, but each is contained and we know the mechanism. Strengthens
**coherence and SOT-conformance** — the things an examiner probes for inconsistency.

### T2.1 — Consolidate the two autoscaler controllers *(Effort M · Risk Med · Thesis HIGH)*
The committed anti-flap (hysteresis + scale-in confirmations) lives on the **`step`**
controller (`autoscaler/decisions.py:156,217-224`), but the **deployed** controller is
**`target`** (`docker-compose.yml:317`) — so the anti-flap is **inert** and `target`
flaps. **Pick one.** Either deploy `step`, or port its anti-flap onto `target`
(`controllers.py decide_target`) and delete the loser. The code currently contradicts
itself; a thesis can't ship two controllers where the good one is switched off.
- Verify: scaling audit no longer alternates out-on-offered / in-on-predicted.

### T2.2 — Move misplaced sidecar business logic to the decision plane *(Effort M · Risk Med · Thesis High)*
The audit found the sidecar hosts logic the SOT says belongs upstream: the quorum guard
(`lb-sidecar/runloop.py:_excluding_would_empty_pool ~455`), the membership/sentinel
guard (`~527-533`), and the confidence gate in `handle_routing`. If the thesis claims a
clean **decision-plane vs data-plane** separation, the sidecar must be a thin renderer.
Move quorum/confidence decisions into the anomaly-detector / policy and leave the
sidecar to *apply* verdicts. (Do this *after* T1.3 so the integration test guards the
move.)

### T2.3 — One source of truth for exclusion state *(Effort M · Risk Med · Thesis Med)*
This week exposed sidecar ↔ detector ↔ `upstream.conf` ↔ `backend_health` desyncs (the
stuck-`down` deadlock had three contributing paths). Formalize **`backend_health` as the
authority**; everything else (conf `down`, sidecar `_excluded`, detector clocks) is
*derived* and reconciled on startup. T0.4/T0.5 are the first half; this is writing it
down as a contract and removing the conf-as-truth path in `_load_state_from_conf`.

### T2.4 — Monotone `cut`-rule tuning (no retrain) *(Effort S · Risk Med · Thesis Med)*
The `cut = 3.0×min_latency ⇒ score×1e-3` rule in
`rl-engine/policies/monotone/policy.py` is the *upstream* cause of weight concentration
(T0.1 bounds the *symptom* downstream). `cut` is a **config param** in the model's
`params.json` — soften it (higher cut, or load-aware) and **re-run the
latency-monotonicity probe** to confirm the property still holds. No model retrain
needed. Decide with the team (they own the retrain) — this is the cheap interim lever.

### T2.5 — Pin the policy single-source-of-truth *(Effort S · Risk Low · Thesis Low)*
`config/policy.yaml` drifts at runtime (policy-manager rewrites `policy_version`,
`slo_p95_latency_ms`). Decide whether the file or the policy-manager state is canonical
and stop the working-tree drift (it muddies commits and reproducibility).

---

## 5. Tier 3 — Higher-risk fixes (the weakest, most-coupled parts)

Bigger blast radius; touch the parts the thesis is most exposed on. Required for
**Track B**, optional-but-valuable for **Track A**.

### T3.1 — Fix the forecast (load coupling) *(Effort L · Risk High · Thesis HIGH)*
**The single highest-value correctness fix.** `forecasting`'s `harmonic_residual`
predicts ~245 rps essentially regardless of live load — that decoupling is the root of
the autoscaler flap (T2.1 only fixes the *controller's* reaction to a bad signal). Until
the forecast tracks offered load, every "adaptive scaling" claim is soft. Options,
safest first:
1. **Blend** the point forecast with live offered-rps + a headroom factor in the scale
   signal (smallest change; arguably belongs in the controller, not the model).
2. **Re-fit** `harmonic_residual` on load-coupled features / retrain with live-rps input.
3. **Replace** with a simple, defensible predictor (EWMA of offered rps + trend) — often
   the honest baseline a thesis should compare against anyway.
- Verify with the **forecast-stress scenario** (§8 S9) and the scaling audit.

### T3.2 — Heterogeneous-capacity benchmark *(Effort M · Risk Med · Thesis HIGH for Track B)*
Today all backends are identical M/G/c queues, so routing *cannot* help by construction
— which is exactly why the homogeneous result is "routing is unhelpful." Build a
scenario with **mixed-capacity backends** (vary `WORKERS` / service time per backend).
This is the *only* honest test of whether learned routing earns its keep. Even under
Track A it sharpens the finding ("routing helps iff backends are heterogeneous").

### T3.3 — Latency-first benchmark *(Effort S · Risk Low · Thesis Med)*
The B_degrade win is mostly **tail latency** (un-ejectable 1800ms backend), but the
harness headlines error rate. Add a latency-SLO-violation metric (fraction of requests
over p95 SLO) so the strongest result is measured on its own terms.

---

## 6. Tier 4 — Riskiest / stretch (new ML, uncertain payoff)

Only after Tiers 0–3 are solid. These are Track-B-defining or beyond-thesis.

### T4.1 — Train + deploy PPO properly, or formally retire it *(Effort L · Risk High · Thesis Med)*
`rl-engine` ships a `ppo` policy (`policies/ppo/`, MaskablePPO/SB3) that is **not
deployed** (`RL_POLICY=monotone` is the default). A half-trained policy sitting in the
tree is a liability — an examiner *will* ask. Either train it to beat the monotone
heuristic on the heterogeneous testbed (T3.2) and deploy it, **or** delete it and state
that a hand-tuned monotone router was sufficient. Don't leave it ambiguous.

### T4.2 — Genuine learned-routing claim (Track B capstone) *(Effort L · Risk High · Thesis HIGH-if-it-works)*
Depends on T3.1 (forecast) + T3.2 (hetero testbed) + T4.1 (PPO). Show the learned policy
beats both static RR **and** the monotone heuristic on heterogeneous load. High payoff,
high risk of a null result — which is *fine* if you've framed Track A as the spine.

### T4.3 — Kubernetes migration *(Effort L · Risk High · Thesis Low)*
The audit's verdict was **no-go** (the control loop wasn't stable enough). Revisit
*only* if everything above is solid and the thesis specifically needs an orchestration
chapter; otherwise cite the audit's reasoning and scope it out. Not recommended for
thesis time.

---

## 7. Master table — everything, safest → riskiest

| ID | Item | Cat | Effort | Risk | Thesis | Deps |
|---|---|---|---|---|---|---|
| T0.1–6 | This session's fix stack | fix | — | — | High | (commit) |
| T1.1 | Ablation benchmark | bench | M | Low | **High** | T0 |
| T1.2 | Stats (mean±CI) in compare.py | bench | S | Low | **High** | — |
| T1.3 | Coupled-loop integration test | test | M | Low | **High** | T0 |
| T1.4 | Regression tests per defect | test | S | Low | Med | — |
| T1.5 | Unit-test hydration fns | test | S | Low | Low | — |
| T1.6 | Fix test_runloop basename clash | test | S | Low | Low | — |
| T1.7 | Methodology + canonical bench doc | doc | M | Low | **High** | — |
| T1.8 | Limitations & future-work doc | doc | S | Low | Med | — |
| T2.1 | Consolidate autoscaler controllers | refactor | M | Med | **High** | T1.3 |
| T2.2 | Move sidecar logic to SOT plane | refactor | M | Med | High | T1.3 |
| T2.3 | One SoT for exclusion state | refactor | M | Med | Med | T2.2 |
| T2.4 | Monotone cut-rule tuning | fix | S | Med | Med | probe |
| T2.5 | Pin policy SoT / stop drift | refactor | S | Low | Low | — |
| T3.1 | Fix forecast (load coupling) | fix | L | High | **High** | T2.1 |
| T3.2 | Heterogeneous-capacity benchmark | bench | M | Med | **High** | — |
| T3.3 | Latency-first benchmark | bench | S | Low | Med | — |
| T4.1 | Train/deploy or retire PPO | impl | L | High | Med | T3.2 |
| T4.2 | Learned-routing claim (capstone) | impl | L | High | High* | T3.1,T3.2,T4.1 |
| T4.3 | Kubernetes migration | impl | L | High | Low | all |

\* high *only if* it produces a positive result; null result is acceptable under Track A framing.

---

## 8. Benchmark scenario catalog

The thesis needs a *suite* of scenarios, each isolating one claim. Current + proposed:

| ID | Scenario | Isolates | Status |
|---|---|---|---|
| S1 | **Hidden bad backend** (slow/503-shedding, `max_fails=0`) — B_degrade | exclusion vs un-ejectable RR | ✅ working (core win) |
| S2 | **Uniform spike** — C_spike | surge robustness, "don't out-think it" | ✅ just recovered |
| S3 | **Slow-but-not-failing** — D_slow | latency reroute without over-exclusion | ✅ ~ties (latency, not errors) |
| S4 | **5v5 (equal-cap) vs 10v5 (scaling)** | routing-only vs routing+autoscaling | ✅ 5v5 fixed; run 10v5 properly |
| S5 | **Heterogeneous capacity** (mixed WORKERS/service-time) | *does learned routing earn its keep?* | ➕ new (T3.2) — the honest routing test |
| S6 | **Correlated / cascading failure** (2–3 backends degrade together) | quorum guard + surge suppression under multi-fault | ➕ new |
| S7 | **Open-loop flash crowd** (Poisson arrivals, not closed-loop) | autoscaler under true demand spikes (no self-throttle) | ➕ new |
| S8 | **Bench→recover churn** (repeated exclude/recover cycles) | no-recovery deadlock fixes + hysteresis | ➕ new (validates T0.4/T0.5) |
| S9 | **Diurnal / periodic load** | the forecast specifically | ➕ new (validates T3.1) |

Each new scenario is ~a half-day of harness work reusing `adaptive-advantage`'s
structure (load shape + organic anomaly schedule + `compare.py`). Run every scenario
with **N≥3 runs and CI** (T1.2).

---

## 9. Recommended sequencing (two tracks)

**Track A — conservative spine (recommended, ~2–3 focused weeks):**
1. Commit Tier 0; confirm RUNS=3 robustness.
2. **T1.2 + T1.1** — stats, then ablation (your headline evidence).
3. **T1.3 + T1.4** — integration + regression tests (reproducibility + safety net).
4. **T2.1** — consolidate the autoscaler (kills the flap story's ambiguity).
5. **T1.7 + T1.8** — methodology + limitations write-up.
6. **T2.2 / T2.3** — SOT-conformance refactors (coherence), guarded by T1.3.
7. **T3.3 + S6/S8** — latency benchmark + robustness scenarios.

**Track B — ambitious extension (only if A is done and time remains):**
8. **T3.1** — fix the forecast (highest-value correctness item).
9. **T3.2** — heterogeneous testbed.
10. **T4.1 → T4.2** — PPO train/deploy → learned-routing claim.

**Defer/scope-out:** T4.3 (k8s), demo/UI work.

---

## 10. Open decisions for you

1. **Track A or A+B?** (drives whether Tiers 3–4 are in scope) — recommend A as spine.
2. **Forecast: blend / re-fit / replace?** (T3.1 options) — recommend *blend* first.
3. **PPO: train-and-deploy or retire?** (T4.1) — don't leave it ambiguous.
4. **Monotone cut-rule: tune now or wait for the team's retrain?** (T2.4).
5. **Autoscaler: deploy `step` or port anti-flap to `target`?** (T2.1).

---

*Generated alongside the control-loop audit (`audit/REPORT.md`) and the spike-recovery
fix stack. Update this file as items land — it is the working plan, not a snapshot.*
