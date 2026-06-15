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

### 3. Fill the result NUMBERS with statistical rigor (the point of #181)
`compare.py` now reports **mean ± 95% CI**. Run it on the canonical 3-run batch and use
those values when filling the placeholder macros (`\BM…` in `preamble.tex`) and the
per-phase results tables — **report mean ± 95% CI, not bare means**:
```
python3 experiments/adaptive-advantage/compare.py \
        experiments/adaptive-advantage/results/20260615T124519Z
```
(The `results/` dir is git-ignored and lives on the benchmark host; if it isn't present in
your environment, the mean values are already in `experiments/adaptive-advantage/README.md`
— add the CIs once the batch is available or re-run the 5v5 RUNS=3 benchmark.)

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
