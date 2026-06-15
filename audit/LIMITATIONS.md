# SmartLoad — Limitations & Future Work

**Purpose:** an honest account of what SmartLoad does *not* yet do well, written so the
thesis can fold it into a Limitations chapter. Every limitation below is stated plainly,
backed by the evidence already on record (`audit/REPORT.md`, `audit/THESIS_ROADMAP.md`,
and the issue tracker), and mapped to the concrete Future-Work item that addresses it.

Owning these weaknesses is itself a contribution: several of them are *findings* (results
about when the approach helps and when it does not), not merely bugs awaiting a patch.

**Sources:** `audit/REPORT.md` (control-loop audit), `audit/THESIS_ROADMAP.md` (Tracks A/B,
Tiers 0–4). Future-Work IDs (`Tn.n`) refer to roadmap items; issue numbers refer to the
GitHub tracker.

**How to read the mapping:** each limitation ends with a *Future Work* line naming the
roadmap tier/item and the issue that owns the fix. Tier 2 items are localized refactors;
Tier 3–4 items are the higher-risk, Track-B extensions.

---

## Summary table

| # | Limitation | Severity | Status | Future Work |
|---|---|---|---|---|
| L1 | Forecast decoupled from live load | High | Open (root cause; worked around downstream) | T3.1 · #189 |
| L2 | Learned routing ties round-robin on homogeneous pools | n/a (a finding) | Characterised, not "fixable" | T3.2/S5 · #190; T4.2 |
| L3 | Monotone `cut`-rule drives weight concentration | Medium | Bounded downstream, not fixed at source | T2.4 |
| L4 | PPO policy shipped but undeployed | Medium (examiner-facing) | Ambiguous; must resolve | T4.1 · #188 |
| L5 | Autoscaler flap worked around, not fixed | Medium | Pinned/mitigated, not consolidated | T2.1 · #183 |
| L6 | No recovery across process restart (loop fragility) | High (class of bug) | Fixed this session; needs hardening as a contract | T2.3, T1.3 |

---

## L1 — The forecast is decoupled from live load

**What it is.** The forecasting service's `harmonic_residual` model predicts roughly
245 rps essentially regardless of the actual offered load. The prediction is a learned
periodic shape, not a function of what is arriving right now, so it does not track demand.

**Why it matters.** This decoupling is the *root* of the autoscaler flap (L5). Because the
controller sized scale-out on `max(predicted, offered)` but scale-in on `predicted` alone,
and because `confidence_upper` runs at roughly twice `predicted` (about 500 rps even at
idle), the two directions disagree across a backend-count boundary and the pool oscillates
(see `audit/REPORT.md` §1, D1, D11). The forecast also produced a non-physical spike to
`offered 10999 rps needs 7 backends` in one `scaling_audit` sample, which would pin the
pool at maximum. Until the forecast follows offered load, every "adaptive scaling" claim
rests on a controller working around a signal it cannot trust rather than on a sound
forward predictor.

**Evidence.** `forecasts.parquet` shows `confidence_upper` at roughly 2× `predicted`
throughout; `scaling_audit.json` shows the predicted estimate held flat near 243 rps while
offered load was about 507 rps (`audit/REPORT.md` §1.2, D1, D11). The autoscaler-only fix
left the pool held at maximum even at idle precisely because the band stays near 500 rps
regardless of demand (§2.1, "Side-effect — D11 materialized").

**Scope of the limitation.** This is a *signal* problem, not a controller problem. The D1
demand-signal change and the `min_backends` pin (L5) make the controller behave despite the
bad signal; they do not make the signal correct. Strong-scaling and diurnal-load claims
remain soft until the forecast tracks load.

**Future Work.** Roadmap **T3.1** (Tier 3, *Effort L, Risk High, Thesis HIGH*), issue
**#189**. Three options, safest first: (1) **blend** the point forecast with live
offered-rps plus a headroom factor in the scale signal; (2) **re-fit** `harmonic_residual`
on load-coupled features; (3) **replace** it with a simple defensible predictor (EWMA of
offered rps plus trend), which is often the honest baseline a thesis should compare against
anyway. Validate with the diurnal/periodic scenario (S9) and the 10v5 scaling run, asserting
that `scaling_audit` predictions track actual load and the pool no longer flaps.

---

## L2 — Learned routing ties round-robin on homogeneous pools (a finding, not a bug)

**What it is.** On a pool of identical backends, SmartLoad's per-backend weighted routing
provides no measurable advantage over plain NGINX round-robin, and under overload the
routing skew is actively harmful (weight concentration leads to queue overflow, which the
detector then mis-scores as illness and benches, producing a self-benching cascade). This is
the C_spike result: with equal-capacity backends the two routers tie.

**Why this is a finding, and how to frame it.** When all backends are identical M/G/c
queues, an even split *is* the optimal split, so a learned policy has nothing to improve and
can only add variance or skew. The tie is therefore the *expected* and *correct* result on
homogeneous load, and it should be presented as a positive characterisation of the method's
operating envelope: **SmartLoad's measurable advantage is anomaly-driven exclusion and
capacity holding/scaling, not fine-grained routing; routing earns its keep only when backend
capacities differ** (`audit/THESIS_ROADMAP.md` §1). Owning this negative result pre-empts
the examiner and converts a pile of fixes into a defensible boundary on the contribution.

**Where routing *does* help today.** The wins are exclusion-driven, not routing-driven: a
hidden bad backend (slow or shedding, which NGINX `max_fails=0` can never eject) is excluded
for roughly 15× fewer errors and roughly 60× better tail latency; a uniform spike is survived
by holding the pool even rather than out-thinking the surge (`audit/THESIS_ROADMAP.md` §1).

**Evidence.** The homogeneous C_spike tie; the documented harm of unbounded skew (queue
overflow → self-benching cascade) and the downstream clamp added to bound it (T0.1/T0.2,
`audit/THESIS_ROADMAP.md` §2). All current backends are identical by construction, so routing
cannot help on the existing benchmark (#190 rationale).

**Future Work.** Roadmap **T3.2 / scenario S5** (Tier 3, *Effort M, Risk Med, Thesis HIGH*),
issue **#190**: build a heterogeneous-capacity benchmark (vary `WORKERS`/service-time per
backend, e.g. 1/2/2/4/4) and compare NGINX RR vs SmartLoad-monotone vs (if available) PPO on
per-window p50/p95/p99 and SLO-violation rate, with N≥3 runs and CI. This is the only honest
test of whether learned routing earns its keep. The capstone claim ("learned routing beats
both RR and the monotone heuristic on heterogeneous load") is **T4.2** and depends on T3.1
(forecast), T3.2 (this testbed), and T4.1 (a deployed PPO). A null result there is acceptable
under the Track A framing, where this limitation is already the spine's finding.

---

## L3 — The monotone `cut`-rule drives weight concentration

**What it is.** The monotone routing policy applies a hard `cut`: a backend whose latency
exceeds 3× the minimum latency has its score multiplied by `1e-3`
(`services/rl-engine/policies/monotone/policy.py`). This near-zeroing of any
slower-than-3×-min backend concentrates routing weight onto the fastest few backends. It is
the *upstream* cause of the weight concentration that the downstream skew clamp (T0.1) only
bounds as a symptom.

**Why it matters.** Concentration sends disproportionate load to a small set of backends,
which raises their queue latency, which can trip the detector's latency channel and bench
them, feeding the cascade described in L2. The clamp (`clamp_weight_skew`, skew fraction 0.75
so the ratio stays at or below 1.33:1, provably below the detector's outlier margin) keeps the
rendered weights from manufacturing an outlier the system's own detector would bench, but it
operates on the *output* of the policy. The decision rule that produces the concentration in
the first place is unchanged.

**Evidence.** The clamp was added specifically because unbounded skew caused
concentration-driven starvation and self-benching (`audit/THESIS_ROADMAP.md` §2, T0.1/T0.2,
T2.4). The `cut` value is a config parameter in the model's `params.json`, so the source
behaviour is tunable without retraining.

**Future Work.** Roadmap **T2.4** (Tier 2, *Effort S, Risk Med, Thesis Med*). Soften `cut` at
the source (a higher cut threshold, or a load-aware cut) and re-run the latency-monotonicity
probe to confirm the policy still satisfies its monotonicity property. No model retrain is
required for the parameter change; coordinate with the team that owns any retrain, since the
clean fix may ultimately want a re-fit rather than a hand-tuned parameter.

---

## L4 — A PPO policy is shipped but not deployed

**What it is.** `rl-engine` ships a `ppo` policy (`services/rl-engine/policies/ppo/`,
MaskablePPO over Stable-Baselines3) that is **not deployed**: the default is
`RL_POLICY=monotone`. A trained-but-unused learned policy sitting in the tree is a liability
because an examiner will ask why it exists and why it is switched off.

**Why it matters.** The ambiguity is examiner-facing. Either the learned policy is part of
the contribution (in which case it must demonstrably beat the heuristic and be deployed) or it
is not (in which case its presence should be explained and scoped to future work). Leaving it
half-finished in the default-off state invites exactly the question the thesis is least
prepared to answer. The existing offline result is that PPO ties round-robin (both at mean
reward −0.0056), which is consistent with L2: on a homogeneous pool there is nothing for a
learned router to learn.

**Future Work.** Roadmap **T4.1** (Tier 4, *Effort L to train or S to retire, Risk Med*),
issue **#188**. Pick one and execute:
- **Retire (recommended for Track A):** quarantine or remove `policies/ppo/` and its
  selection path, and state plainly that a hand-tuned monotone router was sufficient and PPO
  is future work, citing the offline tie. This resolves the ambiguity at low cost.
- **Train and deploy (Track B):** train PPO to beat the monotone heuristic on the
  heterogeneous testbed (L2 / #190), document the evaluation, make it the deployed default,
  and re-run the routing benchmarks, shipping only if it wins.

Whichever path is chosen, the deciding evidence is the heterogeneous benchmark: PPO cannot be
shown to earn its keep on the homogeneous pool where it currently ties.

---

## L5 — The autoscaler flap was worked around, not consolidated

**What it is.** The autoscaler ships **two controllers and deploys the one without an
anti-flap**. The committed anti-flap logic (scale-in confirmations plus hysteresis) lives on
the `step` controller (`services/autoscaler/decisions.py`), but the deployed controller is
`target` (`docker-compose.yml`, `AUTOSCALER_CONTROLLER=target`), which had no anti-flap and
sized scale-out and scale-in on different demand signals. The result was a structural pool
flap (for example 5→3→5→4) that stripped capacity mid-load.

**What was actually done about it.** The flap was *mitigated*, not removed at the design
level. Two interventions reduced its impact: the D1 demand-signal fix (size both scale
directions on `max(predicted, offered)` in `decide_target`, `audit/REPORT.md` §2.1, commit on
record) removed the flap dead-zone and held the container count steady; and the benchmark pins
`MIN_BACKENDS` (the `min_backends` pin, T0.6) to remove the flap and the stale-`down` confound
from the 5v5 A/B comparison. These are correct and load-bearing, but the project still
contains two controllers where the safe one is inert, and the committed `step` anti-flap
remains dead code under the deployed `target` configuration (`audit/REPORT.md` D6). A thesis
cannot cleanly ship two controllers with the safe one switched off.

**Why it matters.** The pin makes the benchmark measure the intended system, and D1 stops the
oscillation, but neither consolidates the codebase to a single coherent controller. The
underlying bad signal (L1) is also still present; D1 makes the controller tolerate it rather
than fixing it.

**Evidence.** `scaling_audit.json` alternation (`offered 507 … needs 5` versus
`predicted 243 … needs 3`), reproduced in three batches and confirmed live at idle
(`audit/REPORT.md` §1.2, D1); the inert-`step`-anti-flap finding (D6); the persistence of the
flap despite the committed hysteresis.

**Future Work.** Roadmap **T2.1** (Tier 2, *Effort M, Risk Med, Thesis HIGH*), issue **#183**.
Keep exactly one controller: port the `step` anti-flap (scale-in confirmations plus
hysteresis) onto `target` with a symmetric demand signal, or switch the deploy to `step`;
remove or deprecate the loser so there is a single code path; and add a regression test that
an alternating `offered`/`predicted` sequence does not produce a 5→3→5 flap within the
cooldown. This must coordinate with the forecast fix (L1 / #189), since the scale signal spans
both services, but it is a distinct change from fixing the signal itself.

---

## L6 — No recovery across a process restart (evidence the coupled loop is fragile)

**What it is.** A class of bugs in which a backend benched (`down;`) before a service restart
could never recover afterward, because the components disagreed about which state was
authoritative. State lived in four places (the sidecar's in-memory `_excluded` set, the
detector's exclusion clocks, the on-disk `upstream.conf`, and the `backend_health` table) and
they desynchronised across restarts. The on-disk `upstream.conf` sits on a persistent named
volume, so a prior run's exclusions survived a restart and were re-imported as fresh
exclusions, while a benched zero-traffic backend emitted no metrics, so the detector never
re-published a `healthy` verdict that would clear it.

**What was fixed this session.** Two changes closed the deadlock (`audit/THESIS_ROADMAP.md`
Tier 0): **T0.4**, detector exclusion-clock hydration from `backend_health` on startup, so a
backend left `down` across a process restart can recover; and **T0.5**, sidecar health
reconciliation that clears a stale conf-inherited `down` when `backend_health` says the
backend is healthy, making `backend_health` (not the stale conf) the restart source of truth.
The related no-recovery-under-load trap (D2) and the conf-re-import-on-restart path (D8) are
documented in `audit/REPORT.md`.

**Why it remains a limitation.** The deadlock is fixed, but the fact that it existed at all is
evidence the coupled control loop is fragile: every real failure this session was *emergent
from coupling* between services, not a defect in any single one (`audit/REPORT.md` §1). Unit
tests structurally cannot catch coupling failures. The fixes (T0.4/T0.5) establish
`backend_health` as the de facto authority but have not yet been written down as a contract,
and the conf-as-truth path in `_load_state_from_conf` still exists as a fallback. The system
is correct today but under-guarded against regression.

**Evidence.** Stale `down;` retained across restart with zero `down;`→active transitions, and
a live idle pool still carrying `server smartload-test-backend-1:8080 down;` while all
containers were Docker-healthy (`audit/REPORT.md` D2, D8); the documented three contributing
paths to the stuck-`down` deadlock (`audit/THESIS_ROADMAP.md` T2.3).

**Future Work.** Two complementary roadmap items. **T2.3** (Tier 2, *Effort M, Risk Med*):
formalise `backend_health` as the single authority for exclusion state, derive everything else
(conf `down`, sidecar `_excluded`, detector clocks) from it, reconcile on startup, and remove
the conf-as-truth path; T0.4/T0.5 are the first half, T2.3 writes it down as a contract.
**T1.3** (Tier 1, *Effort M, Risk Low, Thesis HIGH*): a compose-based coupled-loop integration
test that drives the spike and asserts the invariants unit tests cannot (no cascade, pool at or
above quorum, no healthy backend stuck `down`, errors below threshold). This is the single most
valuable test artifact in the project and doubles as reproducibility evidence; **T1.4** adds a
focused regression test per fixed defect (including this restart deadlock) so it cannot
silently return.

---

## Cross-cutting note: the coupling thesis

Five of these six items trace to one structural property: SmartLoad's control loop is a set of
independent services whose decisions couple, and the system's behaviour is emergent from that
coupling rather than resident in any one component. The forecast feeds the autoscaler (L1→L5);
the routing policy feeds the detector that benches the backends it concentrates load onto
(L3→L2); exclusion state is shared across four stores that must agree (L6). The audit
demonstrated this empirically: no single-component fix moved the error rate, and a
detector-only fix actually regressed it because the autoscaler flapped underneath
(`audit/REPORT.md` §2.2). The honest framing for the thesis is that **the contribution is a
*coupled* anomaly-exclusion-plus-scaling loop with a characterised operating envelope**, and the
Future-Work items above are the path from "works and is understood" to "works, is proven, and is
guarded against regression."
