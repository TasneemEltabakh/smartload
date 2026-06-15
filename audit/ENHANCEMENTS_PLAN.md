# SmartLoad — Enhancement-Exploration Plan (de-risked, branch-ready)

**The bet:** finalize the thesis as **Track A now** (don't wait on any of this), and pursue
the system enhancements **on separate branches in parallel**. Run → measure → **only fold a
result back into the thesis/demo/slides if the numbers earn it.** You can't lose: if a
branch nulls out, the thesis is already done; if it wins, you upgrade a finished thesis with
stronger evidence.

## Why this is safe + cheap to act on
- **Each enhancement = its own branch off `main`**, owning disjoint files (the
  `audit/NEXT_TASKS.md` §4 ownership map already makes them conflict-free).
- **Decision gate** on every branch: do NOT pre-write the upgraded claim — measure first.
- **A null result is a *confirmation* of Track A, not a failure** (e.g. "routing still ties
  RR" *proves* the homogeneous-optimality finding).
- **The upgrade path is already wired**, so folding a win back is minutes, not a rewrite:
  - Demo-UI is **data-only** — inject a new suite into
    `tools/demo-ui/web/public/results/results.json` per `tools/demo-ui/RESULTS_INJECTION_GUIDE.md`.
  - The thesis has the **A+B contingency note** (`audit/THESIS_UPDATE_PROMPT.md`, edit #1) —
    *qualify, don't delete*.
  - `compare.py` gives **mean ± 95% CI**, so any new benchmark reports rigor automatically.
- **No invention, ever:** the demo/thesis only get *real committed* numbers.

---

## The three, ordered by positioning-payoff

Only #190 changes a **headline claim**; the scaling pair turns a **limitation into a result**;
the forecast fix is the **root-cause enabler**. Run them in this order — #190 first (fastest
signal, biggest payoff).

### 🥇 Rank 1 — #190 Heterogeneous benchmark (can flip routing → A+B)
*Payoff: the only one that flips a headline — routing "ties RR" → "beats RR when backends differ."*
> Start a NEW branch off `main`: `git checkout -b enh/heterogeneous-bench`. Work GitHub issue
> **#190** (read it first). Build `experiments/heterogeneous-bench/` with **mixed-capacity
> backends** (vary `WORKERS`, e.g. 1/2/2/4/4) — reuse the `adaptive-advantage` structure **and
> the `smartload-locust:latest` image** (build-if-missing, NO per-side pip). Compare NGINX
> round-robin vs SmartLoad (monotone) on p50/p95/p99 + SLO-violation rate, **RUNS≥3 with mean ±
> 95% CI** (`compare.py`). Touch only the new dir + the `test-backend` compose block.
> **DECISION GATE:** if SmartLoad's routing **measurably beats RR** on heterogeneous backends →
> A+B upgrade: apply the A+B contingency in `audit/THESIS_UPDATE_PROMPT.md` (*qualify, don't
> delete*) and inject a "heterogeneous routing" suite into the demo-ui `results.json`. If it
> **ties** → record the null in `audit/LIMITATIONS.md` (it *confirms* Track A); do NOT change
> the claim. Open a PR linking #190.

### 🥈 Rank 2 — #183 → #185 Autoscaler fix + clean 10v5 (limitation → scaling result)
*Payoff: turns the autoscaler-flap limitation into an "adaptive scaling works" result (RQ4).*
> Start a NEW branch off `main`: `git checkout -b enh/autoscaler-scaling`. **First #183** (read
> it): consolidate the two autoscaler controllers — port the `step` anti-flap onto the deployed
> `target`, symmetric scale-in/out demand signal, delete the loser, add a flap regression test.
> Touch only `services/autoscaler/**` + the `autoscaler` compose block. **Then, same branch,
> #185:** run the clean 10v5 — `MAX_BACKENDS=10 MIN_BACKENDS=1 STEADY_USERS=70 SPIKE_USERS=110
> RUNS=3 bash experiments/adaptive-advantage/run.sh` — capture per-phase + `scaling_audit.json`.
> **DECISION GATE:** if the pool no longer flaps **and** scale-out measurably absorbs the spike
> (10v5 beats 5v5 under load) → inject the 10v5 suite into the demo-ui and upgrade the thesis
> (RQ4 / scaling: Future-Work → a *result*; update README + LIMITATIONS). If it doesn't help /
> still churns → keep scaling as Future Work, record why. Open PRs linking #183 and #185.

### 🥉 Rank 3 — #189 Forecast load-coupling (root cause; strengthens Rank 2 + kills the top limitation)
*Payoff: indirect — makes the scaling result credible and flips limitation L1 (open → fixed).*
> Start a NEW branch off `main`: `git checkout -b enh/forecast-coupling`. Work **#189** (read
> it): make the scale signal **track live offered-rps** (blend first) so the autoscaler stops
> flapping at the source. Validate with a periodic-load scenario + the 10v5 run; add a
> *rising-load → rising-forecast* test. Touch only `services/forecasting/**` (+ tests);
> coordinate with #183. **DECISION GATE:** if the forecast tracks load and the 10v5 result (#185)
> comes out clean → flip **L1** in `audit/LIMITATIONS.md` (open → fixed) and, if the 10v5 numbers
> improve, update the thesis. If the forecast stays unreliable → leave L1 as a documented
> limitation. Open a PR linking #189.

---

## If a branch wins — how to fold it back (cheap, in this order)
1. **Demo-UI** (data-only): inject the new suite into
   `tools/demo-ui/web/public/results/results.json` per the injection guide (real numbers only).
2. **Thesis**: apply the matching contingency — A+B note for #190, RQ4-Future-Work→result for
   #185, L1-open→fixed for #189 — *qualify, don't rewrite*. Report numbers as **mean ± 95% CI**
   (`compare.py`).
3. **Slides / demo walkthrough**: add the new chart/number.

## Guardrails
- Separate branch per enhancement; disjoint file ownership (no conflicts).
- **No invention** — only values you can point to a committed `file:line` for.
- **Time-box** each (you're not depending on them).
- Don't merge an enhancement into the thesis path unless its numbers are positive *and* you
  decide to upgrade.

*Companion to `audit/NEXT_TASKS.md` (the full task map + ownership), `audit/THESIS_ROADMAP.md`
(Track A vs A+B), and `audit/THESIS_UPDATE_PROMPT.md` (the A+B contingency).*
