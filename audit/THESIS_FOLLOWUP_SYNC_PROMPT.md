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

### 3. Fill the `[BENCH:…]` placeholder macros from committed artifacts (NO invention)
The thesis prints loud red `[BENCH:<metric>]` markers (the `\bmtodo{}` macros in
`preamble.tex`) wherever a real measured number is missing. **Fill them from the committed
benchmark reports — do not invent any number.** Map each macro family to its source:

| Macro family | Source (committed) |
|---|---|
| **adaptive-advantage** per-phase + overall | `compare.py` on the 3-run batch (below) — report **mean ± 95% CI** (#181) |
| `an-trendrule-*` (`\BManFone`, `\BManRecall`, `\BManPrecision`, `\BMifFone`) | `experiments/anomaly-detection-bench/REPORT.md` (gradual F1 **0.000→0.845**, recall **0.791**, spike/held-out F1, IF baseline) |
| `fc-*` (`\BMfcMape`, `\BMfcMapeNaive/MA/Arima`, `\BMfcCiCov`, `\BMfcSlaGain`) | `experiments/forecasting-engine-bench/` + `forecasting-downstream-bench/` (REPORT + `results/*/SUMMARY.md`) |
| `rl-*` (`\BMrlPpoReward`, `\BMrl{Monotone,Ppo,Roundrobin}SlaViol`) | `experiments/rl-routing-bench/REPORT.md` — **reconcile framing per edit #1**: offline/open-loop figures, NOT a "monotone beats RR closed-loop" claim |
| `as-*` (`\BMasSlaReactive`, `\BMasSlaTarget`) | `experiments/autoscaler-strategy-bench/REPORT.md` (**77.2% → 98.3%**) |

For the adaptive-advantage numbers, run and use **mean ± 95% CI** (this is #181's payoff):
```
python3 experiments/adaptive-advantage/compare.py \
        experiments/adaptive-advantage/results/20260615T124519Z
```
(`results/` is git-ignored / on the benchmark host; if absent, the means are in
`experiments/adaptive-advantage/README.md` — add CIs once the batch is available.)

**TWO families have NO committed data — do NOT fill them; mark Future Work / cut the numeric
claim** (the same two suites the demo-ui left pending):
- `ad-*` (`\BMadPoolHi/Lo`, `\BMadRunId`, `\BMadScaleActions`) — the **RQ4 adaptive-scaling**
  run has no committed metrics; RQ4 is Future Work under the equal-capacity scope.
- `an-agreement-*` (`\BManAgree`, `\BManAgreeCells`, `an-if-threshold-agreement`) — the
  **engine-agreement %** isn't committed (`anomaly-engine-bench` ships only a README; its
  `F1=0.8012` is a *training* metric, not the agreement number). Reframe or drop; don't fabricate.

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
