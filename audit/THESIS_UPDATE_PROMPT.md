# Thesis-update prompt — reconcile the SmartLoad thesis with the new results

**How to use:** paste the prompt block below into a fresh Claude Code session pointed at
`/home/tasneem/smartload` on `main`. It is self-contained. The thesis is being actively
finalized — **locate edit sites by the QUOTED TEXT, not line numbers** (they drift), and
**coordinate**: if another agent is editing `thesis/` concurrently, take one chapter at a
time and pull before each.

> This was produced from a full read-through of all chapters vs the new canonical
> benchmark (`experiments/adaptive-advantage/`), the ablation (`.../ABLATION.md`), and the
> control-loop audit (`audit/REPORT.md`). The thesis uses **placeholder macros**
> (`\BMxxx` in `thesis/report/preamble.tex`) for numbers, so the staleness is **the
> results NARRATIVE**, not digits — and in several places the draft **contradicts** the
> new ground truth.

---

## THE PROMPT

You are updating the LaTeX thesis under `thesis/report/` to match SmartLoad's new
canonical results. The benchmark numbers themselves are placeholder macros (filled
last); your job is the **narrative**: fix claims that now CONTRADICT the evidence,
re-point to the new benchmark, and add the decisive result the thesis currently omits.
**Do not invent numbers** — use the values below or leave the existing `\BM…` macros.
Read these sources first: `experiments/adaptive-advantage/README.md`,
`experiments/adaptive-advantage/ABLATION.md`, `audit/REPORT.md`, `audit/THESIS_ROADMAP.md`.

### New ground truth (the canonical result)
- Canonical benchmark = **`adaptive-advantage`, 5v5 EQUAL-CAPACITY, 3 runs, clean pinned
  pool.** The old `baseline-vs-smartload` bench **TIED and is SUPERSEDED** (its 50-user
  closed loop self-throttled — a benchmark artifact).
- Per-phase, baseline NGINX round-robin → SmartLoad:
  - **B_degrade** (hidden bad backend, 503-shedding, `max_fails=0` so RR can't eject):
    err **6.53% → 1.21%**, p99 **56,000 ms → 223 ms (~250×)** — **decisive win**.
  - **D_slow** (slow-but-200-OK backend): err **3.78% → 0.97%**, p99 **14,000 → 377 ms
    (~37×)** — **win**.
  - **C_spike** (sudden uniform spike): err **1.82% (RR) vs 3.75% (SL)** — **≈ TIE, RR
    slightly ahead**; p95 220 vs 580 ms.
  - **Overall**: err **1.60% vs 1.32%**; p99 **4,867 vs 623 ms (~8×)**.
- **Ablation** (Δ C_spike err% when a fix is removed): anti-concentration routing clamp
  **+15.0 (dominant)**, equal-capacity pin **+1.8**, per-side reset **+0.7**, #3
  absolute-overload guard **−0.4 (insurance)**.
- **The honest thesis claim (Track A):** SmartLoad's measurable value is **anomaly-driven
  EXCLUSION + capacity holding**, NOT fine-grained learned routing. On a **homogeneous,
  equal-capacity pool under uniform overload, even-split round-robin is provably
  optimal**, so learned/heuristic routing **cannot beat RR** (C_spike tie). The earlier
  catastrophic ~56% "spike collapse" was a **coupled-failure / contamination artifact**
  (autoscaler flap + weight concentration + benching cascade), now **fixed and robust
  across 3 runs** — not an inherent property. **Scope: equal-capacity only**; scaling /
  10v5 and PPO are **Future Work**. PPO is implemented but **NOT deployed**
  (`RL_POLICY=monotone`) — frame it as a retired/honest-null comparator.

### Edits, in order (★ = the draft CONTRADICTS the new truth — must change, not just add)

1. **★ Kill "the monotone router / SmartLoad beats round-robin on a homogeneous /
   closed-loop bench" EVERYWHERE.** It is false now (RR is optimal under uniform
   overload; C_spike 1.82% RR vs 3.75% SL). Sites (find by quoted text):
   - `04b_impl_decision.tex` — *"A monotone, capacity-aware rule therefore beats
     round-robin on the objective by a wide margin"* and the `monotone violates … of a
     homogeneous run` passage; the `imb:ppo_finding` / `imb:iteration` "monotone wins /
     PPO worst on the closed-loop bench" passages.
   - `05_discussion_impact.tex` — *"the strong contender in the closed-loop routing
     bench: on a homogeneous workload it holds the SLA-violation rate to … against …
     round-robin."*
   - `06_conclusion.tex` — *"deployed capacity-aware monotone heuristic holds SLA
     violations to … against … round-robin closed-loop"* and any *"beats round-robin /
     beats the strong baseline."*
   - `03b_design_solution.tex` — *"worst contender on the closed-loop bench"* / the
     engines table headline and *"clear winner in numbers."*
   - `frontmatter/abstract_en.tex` — *"holds SLA violations well below the round-robin
     baseline."*
   **Replace with:** under uniform overload on an equal-capacity pool, even-split
   round-robin is provably optimal, so learned/heuristic routing **ties** it; SmartLoad's
   measurable advantage is **anomaly-driven exclusion + capacity holding**. (Keep the
   open-loop **PPO ≈ RR** null result wherever stated — it is correct.)

   > ### ⚙️ A+B CONTINGENCY — apply edit #1 in QUALIFY mode ONLY IF the heterogeneous benchmark (#190) returns POSITIVE
   > **Default = Track A (above): delete the "beats RR" claim.** Do this unless the
   > heterogeneous-capacity benchmark (issue #190 / `experiments/heterogeneous-bench`) has
   > been run **and shows learned/capacity-aware routing measurably beating round-robin on
   > mixed-capacity backends.** If — and only if — that result exists and is positive,
   > switch to **A+B**: do NOT delete the "beats RR" claim — **QUALIFY it.** The unified
   > claim becomes a conditional: *routing intelligence helps **iff** backends differ; on
   > identical (homogeneous) backends, even-split RR is optimal and the win is exclusion.*
   > Concretely, in A+B mode:
   > - Edit #1 sites: instead of "ties RR," write **"ties RR on a *homogeneous* pool, and
   >   beats RR by «X% tail / «Y SLA-pp on a *heterogeneous* pool"** (numbers from #190).
   > - Edit #3 (results): add a **second results table — heterogeneous capacity — beside
   >   the equal-capacity one** (the homogeneous *tie* and the heterogeneous *win*, side by
   >   side). This contrast *is* the A+B contribution.
   > - **RQ2** may flip from null → "learned routing wins *on heterogeneous load*" (qualify
   >   it; only if a trained policy actually wins — otherwise keep the monotone heuristic as
   >   the winner and PPO as the offline null).
   > - **Abstract / intro / conclusion:** upgrade the one-line claim to the conditional;
   >   shrink Future Work accordingly (the hetero testbed is now done).
   > - **Do NOT** invent heterogeneous numbers — if #190 hasn't run, stay in Track A.

2. **★ Retire the `baseline-vs-smartload` bench; make `adaptive-advantage` (5v5
   equal-capacity, RUNS=3) canonical; re-point RQ1 & RQ3.**
   - `04c_impl_testing.tex` harnesses table — mark `baseline-vs-smartload` **superseded**
     (the 50-user closed-loop tie is an artifact), `adaptive-advantage` canonical;
     rewrite the routing-bench subsection around the equal-capacity methodology (drive
     past the queue knee `STEADY_USERS=70 > QUEUE_MAX=64`, `max_fails=0`, organic
     detection, `MIN_BACKENDS=5` pin); **delete "heterogeneity drag" / "heterogeneous
     run"** wording.
   - `01_introduction.tex` RQ1/RQ3 — re-point to `adaptive-advantage`; **scope RQ1 to
     equal capacity** (no scaling claim). RQ3 (slow-but-200-OK backend RR can't eject) is
     now the **strongest** result — attach the B_degrade/D_slow exclusion win.
   - `06_conclusion.tex` limitations/future — replace "baseline-versus-SmartLoad
     campaign" with the `adaptive-advantage` RUNS=3 results.

3. **★ Replace BOTH per-phase results tables with the new 5-phase numbers.** Old phase
   taxonomies (`A_bootstrap/B_forecast_burst/C_sustain/…` and `A_ramp/A_hold/B_anomaly/…`)
   are wrong → use **A_ramp / B_degrade / C_spike / D_slow / E_tail** with the numbers
   above. Sites: `04c_impl_testing.tex` (phase plan + observed tables) and
   `05_discussion_impact.tex` (`tab:disc:perphase`).

4. **ADD the decisive exclusion + capacity-holding result as the HEADLINE** (currently
   absent everywhere): the abstract results paragraph, `01_introduction.tex` §results,
   `05_discussion_impact.tex` honest-claim box, `06_conclusion.tex` contributions table.
   ~5× fewer errors, 8–250× better tail, at equal capacity, robust across 3 runs.

5. **ADD the ablation decomposition** (the anti-concentration clamp is the dominant
   C_spike fix, +15.0% errors if removed; pin +1.8; guard ≈0 insurance) + the narrative
   that the ~56% collapse was a fixed artifact — to `04c` and `05`. This is the most
   defensible contribution and is currently missing.

6. **★ Fix the routing-risk framing in `03a_design_problem.tex`** — *"on a heterogeneous
   mix, plain round-robin is already a tough opponent"* is backwards. Correct: RR is
   provably optimal on the **homogeneous equal-capacity** pool (the actual scope);
   routing could only help on a heterogeneous pool (Future Work); the value is exclusion
   + capacity.

7. **★ Reconcile the `max_fails` discrepancy in `04a_impl_overview.tex`** — the documented
   data-plane `max_fails=3` conflicts with the methodology the new results rely on
   (`max_fails=0`, so a slow/503-shedding backend is un-ejectable by RR — the basis of
   the B_degrade win). Note the bench uses `max_fails=0` to isolate the exclusion result,
   or correct the config description.

8. **Add honesty caveats to the autoscaler & forecast claims** (don't delete; pending the
   Future-Work fixes): the deployed **`target` controller is the flapping one**, the
   committed anti-flap sits on the unused `step` controller (audit D1/D6; `#183`); the
   **`harmonic_residual` forecast is load-decoupled** (~245 rps regardless of load),
   the root of the flap (audit D11; `#189`). Soften/flag the `\BMfcSlaGain` downstream-SLA
   claim. Sites: `04b_impl_decision.tex` autoscaler+forecast sections, `03b` engines table,
   `05` autoscaler-strategy passage.

9. **State PPO's disposition explicitly** — implemented but **NOT deployed**
   (`RL_POLICY=monotone`), to be **retired / documented as Future Work** (keep the
   open-loop PPO≈RR null). Sites: `06_conclusion.tex` limitations/future; check `03b`/`04b`.

10. **Fill the placeholder macros LAST** (`preamble.tex`) once the corrected narratives
    are in — but note the routing SLA macros (`\BMrlMonoSla`/`\BMrlRrSla`) currently
    underpin the contradicted "beats RR" claim, so their **usage must be reframed (edit
    #1) before any number goes in.** Resolve `% TODO(vps-rerun)` and `\bmfig{}` plots.

### Constraints
- Preserve the macro/placeholder system; don't hard-code numbers that have a macro.
- Don't fabricate values beyond those given here.
- `02_literature_review.tex` is already aligned (it states PPO→RR-equivalence and
  homogeneous-optimality) — minimal/no change.
- **Most affected:** `04c_impl_testing.tex` and `05_discussion_impact.tex` (the superseded
  bench is their spine), then `04b`, `06`, the abstract, `03b`. Work in that priority.
- Open a PR to `main` per chapter-group; keep edits surgical and quote-anchored.

---

*Source analysis: full chapter read-through, 2026-06-15. Numbers from
`experiments/adaptive-advantage/README.md` (3-run, 5v5) + `ABLATION.md`.*
