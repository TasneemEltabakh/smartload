# Thesis follow-up prompt — sync with `main` and use the merged Phase-1 work

**Send this to the thesis agent/session AFTER it finishes the `THESIS_UPDATE_PROMPT.md`
narrative pass.** While it was working, the Phase-1 supporting tasks merged into `main`
(the methodology + limitations source docs, the CI tooling, the A+B note). Its branch
predates those, so it must sync and use them. This is **reconcile + enrich + fill
numbers** on top of the narrative pass — NOT a redo.

---

## THE PROMPT

You finished the thesis-narrative correction (`audit/THESIS_UPDATE_PROMPT.md`). Meanwhile,
supporting work merged into `main` that your branch doesn't have yet. Sync and use it.

### 1. Sync with main (conflict-free)
The merged work touches only NON-thesis files (`audit/*.md`,
`experiments/adaptive-advantage/compare.py`, `tools/demo-ui/*`,
`audit/THESIS_UPDATE_PROMPT.md`) — nothing under `thesis/**` — so the merge is clean.
- Commit your current thesis edits first.
- `git fetch origin && git merge origin/main` (or rebase onto `origin/main`). Resolve the
  trivially (you shouldn't get any `thesis/**` conflicts).

### 2. Enrich the two chapters from the now-available source docs
- **Methodology** (`04c_impl_testing.tex`): cross-check and **fold in anything missing**
  from **`audit/METHODOLOGY.md`** — the queue-knee math (`QUEUE_MAX=64`,
  `WORKERS+QUEUE_MAX=66` admission ceiling, `STEADY_USERS=70 > QUEUE_MAX=64`), the
  closed-vs-open-loop self-throttle (why the old `baseline-vs-smartload` 50-user tie is a
  *superseded artifact*), `max_fails=0` (so RR can't eject a shedding backend), the
  5-phase shape and what each phase isolates, the 5v5/10v5 + pin/reset *test hygiene*, and
  the reproducibility checklist.
- **Limitations & Future Work** (`06_conclusion.tex` / its limitations section):
  cross-check and fold in from **`audit/LIMITATIONS.md`** — L1 forecast decoupling
  (→#189), L2 routing-ties-RR-on-homogeneous *as a finding* (→#190), L3 monotone
  cut-rule, L4 PPO implemented-but-undeployed (→#188), L5 autoscaler flap worked-around
  (→#183) — each with its Future-Work mapping.

### 3. Fill the `[BENCH:…]` placeholder macros — **SPAWN A DEDICATED AGENT for this**
The thesis prints loud red `[BENCH:<metric>]` markers (the `\bmtodo{}` macros in
`preamble.tex`) wherever a real measured number is missing. This fill is **surgical and
isolated to `preamble.tex`**, so **do NOT do it inline — spawn a dedicated sub-agent** for
it (keep it separate from the prose work). Use your Agent/Task tool to launch ONE agent with
the self-contained prompt below, then continue to step 4 while/after it runs:

> **[SPAWN THIS AGENT — macro fill]**
> Fill the SmartLoad thesis's `[BENCH:*]` placeholder macros in `thesis/report/preamble.tex`
> with REAL values from committed benchmark reports — **NO invention**. For each `\bmtodo{…}`
> / `\BM…` macro, find its value in the mapped source file and replace the placeholder.
> **Touch ONLY `thesis/report/preamble.tex`.** Branch off `main`, open a PR when done.
>
> Sources to fill FROM (only commit a value you can point to a line in a committed file for):
> - **adaptive-advantage** per-phase/overall → run `python3 experiments/adaptive-advantage/compare.py experiments/adaptive-advantage/results/20260615T124519Z` and use **mean ± 95% CI** (#181); if `results/` is absent, means are in `experiments/adaptive-advantage/README.md`.
> - `an-trendrule-*` (`\BManFone`,`\BManRecall`,`\BManPrecision`,`\BMifFone`) → `experiments/anomaly-detection-bench/REPORT.md` (gradual F1 **0.000→0.845**, recall **0.791**, spike/held-out F1, IF baseline).
> - `fc-*` (`\BMfcMape`,`\BMfcMapeNaive/MA/Arima`,`\BMfcCiCov`,`\BMfcSlaGain`) → `experiments/forecasting-engine-bench/` + `forecasting-downstream-bench/` (REPORT + `results/*/SUMMARY.md`).
> - `rl-*` (`\BMrlPpoReward`,`\BMrl{Monotone,Ppo,Roundrobin}SlaViol`) → `experiments/rl-routing-bench/REPORT.md` — these are **offline/open-loop** figures that support the **PPO ≈ RR null**, NOT a "monotone beats RR" claim.
> - `as-*` (`\BMasSlaReactive`,`\BMasSlaTarget`) → `experiments/autoscaler-strategy-bench/REPORT.md` (**77.2% → 98.3%**).
>
> **DO NOT fill these two families — leave as-is / mark Future Work (no committed data, no fabrication):**
> - `ad-*` (`\BMadPoolHi/Lo`,`\BMadRunId`,`\BMadScaleActions`) — RQ4 adaptive-scaling; Future Work under the equal-capacity scope.
> - `an-agreement-*` (`\BManAgree`,`\BManAgreeCells`,`an-if-threshold-agreement`) — engine-agreement % not committed (`anomaly-engine-bench` has only a README; its `F1=0.8012` is a *training* metric, not the agreement number).
>
> When done: verify no `\BM…` macro is left undefined and the `[BENCH:*]` count dropped; report which macros you filled (with the source file:line for each) and which you left as Future Work, plus the new placeholder count.

### 4. A+B contingency
Read the **A+B CONTINGENCY** callout in `audit/THESIS_UPDATE_PROMPT.md` (edit #1). Stay
**Track A** — routing *ties* round-robin; the win is anomaly-driven exclusion — UNLESS the
heterogeneous benchmark (#190) has run and is positive. It has **not**, so do **not** claim
A+B; leave the note as a future hook.

### 5. Finish
Recompile; do a cross-chapter coherence read (abstract ↔ intro ↔ results ↔ discussion ↔
conclusion all telling the same Track-A story — exclusion wins + the honest C_spike tie);
commit and update your PR.

**Do not redo the narrative pass.** This step is: sync → enrich methodology/limitations
from the merged docs → fill numbers with CI → coherence check.
