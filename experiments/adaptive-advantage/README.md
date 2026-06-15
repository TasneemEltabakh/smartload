# adaptive-advantage — a harder SmartLoad vs NGINX-RR benchmark

A well-rounded comparison built to expose the *real* differences between the full
SmartLoad decision plane and plain static NGINX round-robin — where the original
`baseline-vs-smartload` benchmark tied (its 50-user closed-loop load self-throttled
a slow backend, so nothing ever shed and the slowdown hid in the worst-case `max`).

## What it does differently

- **Load is driven past the queue knee** (`STEADY_USERS=70 > QUEUE_MAX=64`) so a
  *severely* degraded backend's queue overflows and **sheds 503**. NGINX runs
  `max_fails=0`, so plain RR can **never eject** it and keeps routing 1/N of traffic
  onto it; SmartLoad's error channel detects it and the sidecar pulls it out.
- **Organic detection** — anomalies are injected with `/_admin/delay` only (no
  manual `/isolate` hint), so SmartLoad's detector must find them itself.
- **A traffic spike** — to exercise autoscaling vs a static pool.
- **Two backends degrade at different times** (severe 503-shedding, then moderate
  slow) — testing the error channel, the latency channel, and continuous adaptation.

### 5-phase load shape (`locust/locustfile.py`)
| Phase | window | event |
|---|---|---|
| A_ramp    | 0–60s    | ramp to STEADY_USERS |
| B_degrade | 60–180s  | backend-1 +SEVERE_MS (organic 503-shed) |
| C_spike   | 180–240s | spike to SPIKE_USERS (backend-1 recovered) |
| D_slow    | 240–360s | backend-2 +MODERATE_MS (slow, not failing) |
| E_tail    | 360–420s | backend-2 recovered; settle |

### Two capacity scenarios
- **Equal-capacity (5v5):** both NGINX and SmartLoad limited to 5 backends
  (`MAX_BACKENDS=5`). SmartLoad can only **reroute**, not add servers — isolates
  pure routing/detection intelligence vs round-robin.
- **Full-system (10v5):** SmartLoad allowed to scale out (`MAX_BACKENDS=10`) vs a
  static 5-backend NGINX — shows total real-world value (adaptation + capacity).

### Run it
```bash
# equal-capacity (routing only)
MAX_BACKENDS=5 STEADY_USERS=70 SPIKE_USERS=110 SEVERE_MS=1800 MODERATE_MS=400 RUNS=2 \
  bash experiments/adaptive-advantage/run.sh
# full-system (scaling allowed)
MAX_BACKENDS=10 ... RUNS=2 bash experiments/adaptive-advantage/run.sh
# SHORT=1 for a ~2-min/side harness validation
python3 experiments/adaptive-advantage/compare.py <results/timestamp-dir>
```

## Results (5v5 equal-capacity, **3 runs**, clean pinned pool — per-phase)

After the fix stack (anti-concentration clamp, `#3` absolute-overload guard,
restart-recovery hydration, `min_backends=5` pin, per-side routing reset). The pin +
reset also de-contaminate the **baseline** (a prior batch had baseline C_spike ~44%
and B_degrade ~9% purely because the autoscaler flap was destroying *its* capacity
too — see "What changed" below).

| Phase | base err% | SL err% | base p99 | SL p99 |
|---|---|---|---|---|
| A_ramp    | 0.00% | 0.00% | 117 ms | 123 ms |
| **B_degrade** | **6.53%** | **1.21%** ✅ | **56,000 ms** | **223 ms** ✅ |
| C_spike   | **1.82%** | 3.75% ⟂ | 317 ms | 690 ms |
| **D_slow**    | **3.78%** | **0.97%** ✅ | **14,000 ms** | **377 ms** ✅ |
| E_tail    | 0.00% | 0.00% | 133 ms | 407 ms |
| **Overall** | 1.60% | **1.32%** | **4,867 ms** | **623 ms** ✅ |

**C_spike is now robust across all 3 runs** (SL = 3.4% / 3.4% / 4.5%) — the previous
run-to-run collapse (one run 5.9%, the next 56%) is gone.

Result batches on disk (kept for reference):
- `results/20260615T124519Z` — the clean 3-run batch above (`MIN_BACKENDS=5`).
- earlier batches (`...114845Z`, `...0953*`) — superseded (autoscaler-flap /
  stale-`down` contaminated); kept for the before/after record.

## Ablation — per-fix contribution (5v5, RUNS=2; `ablation.sh`)

Leave-one-out: each fix removed, one at a time. `Δfull` = the **cost of removing
that fix** on **C_spike** error% (positive ⇒ worse without it).

| Fix removed | C_spike err% | Δ vs full | reading |
|---|---|---|---|
| *(full stack)* | 3.3% | — | baseline RR here = 1.7% |
| **anti-concentration clamp** | **18.3%** | **+15.0** | **dominant — recovers the spike almost single-handedly** |
| equal-capacity pin | 5.1% | +1.8 | secondary — the autoscaler flap costs real capacity |
| per-side reset | 4.0% | +0.7 | minor — stale-`down` carryover |
| `#3` absolute-overload guard | 2.9% | −0.4 | ~0 here — *insurance* that didn't need to fire in this clean run |

**Takeaway:** the **clamp** does the heavy lifting; the **pin** is a clear second; the
`#3` guard is insurance (it earned its keep in the earlier *contaminated* 56% collapse,
where benching cascaded — this clean scenario never gets hot enough to trigger it).
*Caveat:* RUNS=2, so non-C_spike deltas (esp. D_slow) are within run-to-run noise —
re-run with RUNS≥3 + CI (#181) for a thesis-grade table. Batch:
`results/ablation-20260615T135625Z/ablation_report.md`.

## Findings

1. **Decisive win — hidden bad backend (B_degrade).** SmartLoad cut errors
   **6.5% → 1.2%** and tail latency **p99 56,000 ms → 223 ms (~250×)**. A backend that
   is *slow/shedding but never trips `max_fails`* is invisible to round-robin (which
   keeps feeding it 1/N of traffic); SmartLoad detects it organically and excludes it.
   **This is the core thesis claim, robust across 3 runs, at equal capacity.**

2. **Decisive win — slow backend (D_slow).** SmartLoad **3.8% → 1.0%** errors and
   **p99 14,000 ms → 377 ms (~37×)**: it reroutes around the slow backend; static RR
   keeps 1/N of traffic on it.

3. **Uniform spike (C_spike) ≈ ties, RR slightly ahead** (SL 3.75% vs base 1.82%,
   p95 580 vs 220 ms). This is the **expected, honest result**: on a *homogeneous*
   pool under *uniform* overload, an **even split is provably optimal**, so learned
   routing cannot beat round-robin — SmartLoad's bounded skew (≤1.33:1) costs a little.
   Crucially, the earlier **60% "collapse" was not an inherent property** — it was a
   *coupled-failure / benchmark-contamination* artifact (the autoscaler flap killing
   capacity mid-spike, amplified on the SmartLoad side by weight concentration + a
   benching cascade). With those fixed, the spike is a small, stable trail, not a
   collapse.

4. **Aggregate tail latency is SmartLoad's, decisively.** Overall **p99 623 ms vs
   4,867 ms (~8×)** — driven by the exclusion wins (B_degrade, D_slow), where RR
   leaves requests stuck behind un-ejectable bad backends.

## Honest bottom line

SmartLoad's measurable value is **anomaly-driven exclusion + capacity holding**, not
fine-grained routing on a homogeneous pool: it **decisively beats static NGINX at the
failure modes it is built for** (hidden bad backend, slow backend — ~5× fewer errors,
8–250× better tail) and **≈ ties under a uniform spike** (where even-split RR is
optimal and learned routing cannot help). The once-catastrophic spike regression was a
coupling failure, now fixed and robust. See `../../audit/THESIS_ROADMAP.md` for how
this finding shapes the thesis claim, and `ABLATION.md` for the per-fix contribution
study.
