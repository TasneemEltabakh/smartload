# SmartLoad — Next-Tasks Handoff & Prompt Pack

**Purpose:** keep every follow-up session on track after this one ends. Each task below
has a **ready-to-paste prompt** — open a fresh Claude Code session and paste the prompt
to start that task. Full scope/acceptance lives in the linked GitHub issue; the prompt
just launches it correctly (right branch, right file lane, read-the-issue-first).

**Context (what already landed):** the spike-robustness fix stack + the ablation
harness are committed in **PR #184** (`fix/anomaly-overload-exclusion`). Audit =
`audit/REPORT.md`; claim/plan = `audit/THESIS_ROADMAP.md`; benchmark results =
`experiments/adaptive-advantage/README.md`. The RUNS=2 **ablation** is the last thing
this session runs — its per-fix contribution table will be posted to **#180** (closed)
and folded into the benchmark README when it finishes.

**Base branch for all tasks:** `fix/anomaly-overload-exclusion` (PR #184) until it
merges into `main`, then `main`. Always branch off it; open a PR per task linking its
issue.

---

## 0. Decisions only YOU can make (these gate the agent tasks)

| # | Decision | Recommendation | Gates |
|---|---|---|---|
| D1 | **Track A** (exclusion+scaling beats RR; routing ties on homogeneous pools) **vs A+B** (also claim learned routing wins) | **Track A** as the spine | scope of #189, #190, #188-B |
| D2 | **Scaling claim** | ✅ **DECIDED: equal-capacity** — RQ1 scoped to equal capacity; **no scaling claim**. #183 & #185 → Future Work (After). | #183, #185 |
| D3 | **PPO**: retire **or** train+deploy | **retire + document** (Track A) | #188 |
| D4 | **Autoscaler**: deploy `step` **or** port anti-flap to `target` | port to `target` (keeps deployed path) | #183 |
| D5 | **Forecast**: blend / re-fit / replace | **blend** first (smallest) | #189 |

---

## 0b. BEFORE vs AFTER the thesis — **D2 = equal-capacity (locked)**

Scaling is scoped out, so the thesis spine is the **routing/detection wins** (B_degrade,
D_slow) + the **honest C_spike finding**, all at equal capacity. That makes the
finalize-the-thesis list short:

- **BEFORE (do to finalize):** D1 (lock Track A) · D3 (PPO framing) · **#181** (stats) ·
  **#186** (methodology) · **#187** (limitations) · the **ablation table** (running) · the
  audit (✅ done) → then the **§3 reconciliation pass**.
- **AFTER / Future Work (cite, don't block):** **#183** (flap → documented limitation) ·
  **#185** (10v5/scaling) · **#182** (integration test) · **#188** (PPO code retire) ·
  **#189** (forecast) · **#190** (heterogeneous bench) · all optional polish (§2).

---

## 1. Open tasks — with launch prompts

### BEFORE the thesis — critical path (finalize with these; independent, run in parallel)

**#181 · T1.2 — Statistical rigor in `compare.py`**
> Work on GitHub issue **#181** (mean ± 95% CI in `experiments/adaptive-advantage/compare.py`). Read the issue first for the full DoD and the file-ownership rules. Branch off `fix/anomaly-overload-exclusion`. Add per-phase **mean ± stdev / 95% CI** across runs + a significance flag vs baseline; **stdlib `statistics` only** (no numpy/scipy); keep the CLI and the `GET-/-<phase>` parsing contract; single-run input must degrade to `n/a` gracefully. Verify against `results/20260615T124519Z` (3 runs). Touch **only** `compare.py`. Open a PR linking #181.

**#186 · T1.7 — Methodology write-up** *(Methodology chapter source)*
> Work on GitHub issue **#186**. Read the issue first. Write **only** `audit/METHODOLOGY.md` covering closed/open-loop, the queue-knee math (`QUEUE_MAX`/`WORKERS`/`max_fails=0`), the 5-phase shape, 5v5-vs-10v5 + the pin/reset hygiene, organic anomaly injection, and a reproducibility checklist. Source facts from `experiments/adaptive-advantage/*` + `audit/REPORT.md`. Do **not** edit `thesis/**`. Open a PR linking #186.

**#187 · T1.8 — Limitations & Future-Work write-up** *(Limitations chapter source)*
> Work on GitHub issue **#187**. Read the issue first. Write **only** `audit/LIMITATIONS.md`: forecast decoupling (#189), routing-ties-RR-on-homogeneous (#190), monotone cut-rule, undeployed PPO (#188), autoscaler flap worked-around (#183), no-recovery-across-restart class — each mapped to its Future-Work item. Do **not** edit `thesis/**`. Open a PR linking #187.

### AFTER the thesis — Future Work (cite as Future Work; **none gate finalizing**)

**#185 · S4 — Clean 10v5 (scaling) benchmark** — *scoped out by D2 (no scaling claim); future work*
> Work on GitHub issue **#185** (clean 10v5 run). Read the issue first. Run `MAX_BACKENDS=10 MIN_BACKENDS=1 ... RUNS=3 bash experiments/adaptive-advantage/run.sh`, capture per-phase + `scaling_audit.json`, add a clean 10v5 table to `experiments/adaptive-advantage/README.md`, and state plainly whether scale-out helps or the autoscaler flap (#183) churns capacity. **Run after #183 lands.** Touch only the README 10v5 section. Open a PR linking #185.

**#182 · T1.3 — Coupled-loop integration test** — *reproducibility insurance, not thesis content*
> Work on GitHub issue **#182** (spike-invariants integration test). Read the issue first — note `tests/integration/` **already exists** (reuse the `stack_ready` fixture + `_chaos.set_backend_delay` + the `slow` marker; do NOT recreate infra). Branch off `fix/anomaly-overload-exclusion`. Add **only** `tests/integration/test_spike_invariants.py` asserting: no benching cascade (pool ≥ quorum), no stuck-`down` healthy backend, routing skew ≤1.33:1, spike error rate < threshold. Include the teeth check (disable a fix → test fails). Open a PR linking #182.

**#183 · T2.1 — Consolidate the autoscaler controllers** — *the flap is a documented limitation now (#187)*
> Work on GitHub issue **#183**. Read the issue first. The deployed `target` controller flaps; the anti-flap lives on the inert `step` controller. Pick one (recommend: port the `step` anti-flap onto `target`), make scale-in/out use a consistent demand signal, delete the loser, add a flap regression test, validate with the 10v5 run. Touch **only** `services/autoscaler/**`, `tests/unit/autoscaler/**`, and the `autoscaler` block of `docker-compose.yml`. Open a PR linking #183.

**#188 · T4.1 — PPO: retire or train+deploy** — *decision D3 = retire*
> Work on GitHub issue **#188** (decision D3 = retire, recommended). Read the issue first. Option A: quarantine/remove `services/rl-engine/policies/ppo/` + its selection path + compose/docs refs, add a note for `audit/LIMITATIONS.md`. Touch **only** `services/rl-engine/**`, `tests/unit/rl-engine/**`, and the `rl-engine` compose block. Open a PR linking #188.

### Track B / deeper future work (only if you later extend to A+B)

**#189 · T3.1 — Fix the forecast (load coupling)** — highest-value correctness item.
> Work on GitHub issue **#189** (decision D5 = blend first). Read the issue first. Make the scale signal track live offered-rps so the autoscaler stops flapping; validate with a periodic-load scenario + the 10v5 run (#185); add a rising-load→rising-forecast test. Touch **only** `services/forecasting/**` (+ its tests); coordinate with #183. Open a PR linking #189.

**#190 · T3.2/S5 — Heterogeneous-capacity benchmark** — the honest routing test.
> Work on GitHub issue **#190**. Read the issue first. Add `experiments/heterogeneous-bench/**` with mixed-`WORKERS` backends (reuse the adaptive-advantage structure); compare RR vs SmartLoad (vs PPO if trained) on p50/p95/p99 + SLO violations; RUNS≥3 with CI. Touch only the new dir + the `test-backend` compose block. Open a PR linking #190.

---

## 2. Optional polish (no issue — track here; do only if time)
- **T1.4** regression tests per fixed defect · **T1.5** unit-test the hydration fns · **T1.6** fix the `test_runloop.py` basename clash (rename one so the full suite collects in one `pytest`).
- **T2.2/T2.3** move the SOT-forbidden logic out of the sidecar + one source-of-truth for exclusion state · **T2.5** stop `config/policy.yaml` runtime drift.
- **T2.4** tune the monotone `cut` config (+ re-run the monotonicity probe).
- **T3.3** latency-first benchmark · **S6/S7/S8** correlated-failure / open-loop flash-crowd / recover-churn scenarios.
> One prompt for the batch: *"Pick up roadmap item <Txx> from `audit/THESIS_ROADMAP.md` §<n>; read it for scope, branch off `fix/anomaly-overload-exclusion`, keep to its file lane, open a PR."*

---

## 3. The thesis (do LAST — coordinate, don't collide)
A separate agent is writing `thesis/` **without** today's results. **No agent here touches `thesis/`** until that agent finishes. Then, the reconciliation pass:
> After the thesis-writing agent finishes AND **#186 + #187** have landed (D2 = equal-capacity, so **#185 is NOT a gate**): read all of `thesis/report/chapters/` + the abstract/conclusion, and reconcile against today's evidence — fold in `audit/METHODOLOGY.md`, `audit/LIMITATIONS.md`, the `adaptive-advantage` **equal-capacity (5v5)** results (PR #184) + the ablation table, and the `audit/REPORT.md` failure analysis. Produce `audit/THESIS_RECONCILIATION.md` first (chapter → currently-claims → now-true → backing-artifact → keep/update/add), confirm the central claim is **Track A** and **RQ1 is scoped to equal capacity** (scaling → Future Work), then apply the edits to the chapters. Flag any place today's results *contradict* the draft.

---

## 4. File-ownership map (parallel work without conflict)

| Issue | Owns (edit freely) | Must NOT touch |
|---|---|---|
| #181 | `experiments/adaptive-advantage/compare.py` | everything else |
| #182 | **new** `tests/integration/test_spike_invariants.py` | existing `tests/integration/*`, `pytest.ini`, `tests/unit/**`, `services/**`, `experiments/**` |
| #183 | `services/autoscaler/**`, `tests/unit/autoscaler/**`, compose **`autoscaler`** block | `experiments/**`, other services, other compose blocks |
| #185 | `experiments/adaptive-advantage/README.md` (10v5 section), `results/` | `compare.py`, `run.sh`, `services/**` |
| #186 | **new** `audit/METHODOLOGY.md` | `thesis/**`, `services/**`, scripts |
| #187 | **new** `audit/LIMITATIONS.md` | `thesis/**`, `services/**`, scripts |
| #188 | `services/rl-engine/**`, `tests/unit/rl-engine/**`, compose **`rl-engine`** block | other services/blocks, `experiments/**` |
| #189 | `services/forecasting/**`, `tests/unit/forecasting/**` | other services (coordinate w/ #183 on the scale signal) |
| #190 | **new** `experiments/heterogeneous-bench/**`, compose **`test-backend`** block | `adaptive-advantage/` scripts, other compose blocks |

`docker-compose.yml` is shared but every issue edits a **different service block** (autoscaler / rl-engine / test-backend) — disjoint, merges cleanly. No two issues edit the same file. `thesis/**` is off-limits to all of these (owned by the thesis agent + the §3 reconciliation pass).

---

## 5. Suggested order (D2 = equal-capacity)
1. **You:** settle the remaining decisions D1, D3, D4, D5 (§0). *(D2 ✅ done.)*
2. **BEFORE the thesis** — parallel: **#181, #186, #187** (independent, low-risk).
3. **§3 thesis reconciliation** — after the thesis agent finishes + #186/#187 land. *(This finalizes the thesis.)*
4. **AFTER / Future Work** (any time, none gate the thesis): **#182**, **#183 → #185**, **#188**, then *(Track B)* **#189 → #190**.

*Living doc — update statuses as PRs land. Open issues: #181 #182 #183 #185 #186 #187 #188 #189 #190. Closed/done: #180 (ablation, completed inline).*
