# SmartLoad — Next-Tasks Handoff & Prompt Pack

**Purpose:** keep every follow-up session on track after this one ends. Each task below
has a **ready-to-paste prompt** — open a fresh Claude Code session and paste the prompt
to start that task. Full scope/acceptance lives in the linked GitHub issue; the prompt
just launches it correctly (right branch, right file lane, read-the-issue-first).

**Context (what already landed in `main`):** PR **#184** (spike-robustness fix stack +
ablation harness) and PR **#192** (locust image + ablation results) are **merged**; PR
**#193** (demo-ui results) is open — merge it. Audit = `audit/REPORT.md`; claim/plan =
`audit/THESIS_ROADMAP.md`; benchmark results = `experiments/adaptive-advantage/README.md`;
**thesis-update prompt = `audit/THESIS_UPDATE_PROMPT.md`**. The **ablation** is done — the
**anti-concentration clamp is the dominant fix** (removing it costs **+15% C_spike
errors**), pin secondary (+1.8), `#3` guard/reset are insurance.

**Base branch for all tasks: `main`** (PR #184 + #192 merged). Branch off `main`; open a
PR per task linking its issue.

**⚙️ Benchmark tooling — for ANY task that runs or creates a benchmark:** use the
pre-built **`smartload-locust:latest`** image (Dockerfile:
`experiments/adaptive-advantage/locust/Dockerfile`, locust **2.44.3** pinned).
`experiments/adaptive-advantage/run.sh` already auto-builds + uses it. If you write a
**new** harness, **reuse the same image** with a build-if-missing guard — do **NOT**
`pip install locust` per side (slow + network-flaky):
```bash
LOCUST_IMG=smartload-locust:latest
docker image inspect "$LOCUST_IMG" >/dev/null 2>&1 || \
  docker build -q -t "$LOCUST_IMG" experiments/adaptive-advantage/locust
docker run --rm --network smartload_smartload-net -v "$HERE/locust:/locust:ro" \
  -v "$out:/out" "$LOCUST_IMG" locust -f /locust/locustfile.py ...   # no pip install
```

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

- **BEFORE (do to finalize) — see §1 PHASE 1:** the **THESIS UPDATE** (paste
  `audit/THESIS_UPDATE_PROMPT.md`) ‖ **#181** (stats) ‖ **#186** (methodology) ‖ **#187**
  (limitations). Decisions D1 (Track A) + D3 (PPO framing). Inputs ready: ablation table
  (✅ done), audit (✅ done), demo-ui (✅ PR #193).
- **AFTER / Future Work (cite, don't block) — §1 PHASE 3:** **#182**, **#188**, **#183 →
  #185**, **#189 → #190**, optional polish (§2).

---

## 1. Execution order — paste a prompt to start each task

**Legend:** **‖** = runs in PARALLEL with the others in its group (each owns different
files — see the §4 ownership map). **→** = sequential (the next needs the prior).
**Base branch for everything is now `main`** (PR #184 + #192 merged).

### ▶ PHASE 1 — FINALIZE THE THESIS  *(your priority — start now; [A]–[D] all parallel)*

**[A] ‖ THESIS UPDATE — the main task**  *(owns `thesis/**`)*
> Open a fresh session and paste the **full prompt in `audit/THESIS_UPDATE_PROMPT.md`**. It corrects the chapters to the new ground truth: kills the (now-FALSE) "monotone heuristic beats round-robin" claim, retires the superseded `baseline-vs-smartload` bench and re-points RQ1/RQ3 to `adaptive-advantage` (equal-capacity), replaces both per-phase tables with the new numbers, and adds the decisive **anomaly-driven exclusion** headline + the ablation. Locate edits by quoted text (line numbers drift); coordinate if another agent edits `thesis/` (one chapter-group at a time, pull first). **This is the long pole — start it first.**

**[B] ‖ #181 — Statistical rigor in `compare.py`**  *(produces the CI the thesis macros need)*
> Work on GitHub issue **#181**. Read the issue first. Branch off `main`. Add per-phase **mean ± stdev / 95% CI** across runs + a significance flag vs baseline; **stdlib `statistics` only**; keep the CLI + the `GET-/-<phase>` parsing contract; single-run input degrades to `n/a`. Verify against `results/20260615T124519Z` (3 runs). Touch **only** `compare.py`. Open a PR linking #181.

**[C] ‖ #186 — Methodology write-up**  *(Methodology-chapter source; [A] may also pull facts straight from the README/audit)*
> Work on GitHub issue **#186**. Read the issue first. Branch off `main`. Write **only** `audit/METHODOLOGY.md` (closed/open-loop, the queue-knee math `QUEUE_MAX`/`WORKERS`/`max_fails=0`, the 5-phase shape, 5v5-vs-10v5 + pin/reset hygiene, organic anomaly injection, reproducibility checklist). Source from `experiments/adaptive-advantage/*` + `audit/REPORT.md`. Do **not** edit `thesis/**`. Open a PR linking #186.

**[D] ‖ #187 — Limitations & Future-Work write-up**  *(Limitations-chapter source)*
> Work on GitHub issue **#187**. Read the issue first. Branch off `main`. Write **only** `audit/LIMITATIONS.md` (forecast decoupling #189, routing-ties-RR-on-homogeneous #190, monotone cut-rule, undeployed PPO #188, autoscaler flap worked-around #183, no-recovery-across-restart) — each mapped to its Future-Work item. Do **not** edit `thesis/**`. Open a PR linking #187.

### ▶ PHASE 2 — DEMO-UI (the results presentation)  *(✅ already done — just merge)*

**✅ PR #193** updated the demo-ui with the new results + the honest C_spike/limitations framing. **Merge it.** The UI reads ONE data file (`tools/demo-ui/web/public/results/results.json`) per `tools/demo-ui/RESULTS_INJECTION_GUIDE.md` — no component edits. To refresh it again later (e.g. once #181 adds CI, or macros get final numbers):
> Update the SmartLoad demo-ui with new benchmark results — **data only**. Per `tools/demo-ui/RESULTS_INJECTION_GUIDE.md`, edit `tools/demo-ui/web/public/results/results.json` (the `schema.ts`→`adapter.ts`→`load.ts` seam; a suite = systems × configurations × metrics; `value:null` renders PENDING; never edit components). Keep numbers accurate to `experiments/adaptive-advantage/README.md` + `ABLATION.md`, keep the honest framing (exclusion wins; C_spike tie; the limitations). Validate the JSON resolves against the schema; open a PR to `main`.

### ▶ PHASE 3 — AFTER the thesis (Future Work; **NONE gate finalizing**)

Order: **‖ #182** and **‖ #188** anytime · **#183 → #185** · *(Track B)* **#189 → #190** · then optional polish (§2).

**‖ #182 · T1.3 — Coupled-loop integration test**  *(reproducibility insurance)*
> Work on GitHub issue **#182**. Read the issue first — `tests/integration/` **already exists** (reuse the `stack_ready` fixture + `_chaos.set_backend_delay` + the `slow` marker; do NOT recreate infra). Branch off `main`. Add **only** `tests/integration/test_spike_invariants.py` asserting: no benching cascade (pool ≥ quorum), no stuck-`down` healthy backend, routing skew ≤1.33:1, spike error rate < threshold. Include the teeth check (disable a fix → test fails). Open a PR linking #182.

**‖ #188 · T4.1 — PPO: retire (D3)**
> Work on GitHub issue **#188** (D3 = retire). Read the issue first. Branch off `main`. Quarantine/remove `services/rl-engine/policies/ppo/` + its selection path + compose/docs refs; add a note for `audit/LIMITATIONS.md`. Touch **only** `services/rl-engine/**`, `tests/unit/rl-engine/**`, the `rl-engine` compose block. Open a PR linking #188.

**#183 · T2.1 — Consolidate the autoscaler controllers**  *(→ unblocks #185)*
> Work on GitHub issue **#183**. Read the issue first. Branch off `main`. The deployed `target` controller flaps; the anti-flap lives on the inert `step` controller. Port the `step` anti-flap onto `target`, make scale-in/out use a consistent demand signal, delete the loser, add a flap regression test. Touch **only** `services/autoscaler/**`, `tests/unit/autoscaler/**`, the `autoscaler` compose block. Open a PR linking #183.

**#185 · S4 — Clean 10v5 (scaling) benchmark**  *(run AFTER #183)*
> Work on GitHub issue **#185**. Read the issue first. Run `MAX_BACKENDS=10 MIN_BACKENDS=1 ... RUNS=3 bash experiments/adaptive-advantage/run.sh` (uses the `smartload-locust` image automatically), capture per-phase + `scaling_audit.json`, add a clean 10v5 table to `experiments/adaptive-advantage/README.md`, state whether scale-out helps or the flap churns capacity. Touch only the README 10v5 section. Open a PR linking #185.

**#189 · T3.1 — Fix the forecast (load coupling)**  *(Track B; → #190)*
> Work on GitHub issue **#189** (D5 = blend first). Read the issue first. Branch off `main`. Make the scale signal track live offered-rps so the autoscaler stops flapping; validate with a periodic-load scenario + #185; add a rising-load→rising-forecast test. Touch **only** `services/forecasting/**` (+ tests); coordinate with #183. Open a PR linking #189.

**#190 · T3.2/S5 — Heterogeneous-capacity benchmark**  *(Track B; the honest routing test)*
> Work on GitHub issue **#190**. Read the issue first. Branch off `main`. Add `experiments/heterogeneous-bench/**` with mixed-`WORKERS` backends (reuse the adaptive-advantage structure **and the `smartload-locust:latest` image** — build-if-missing, NO per-side `pip install`); compare RR vs SmartLoad on p50/p95/p99 + SLO violations; RUNS≥3 with CI. Touch only the new dir + the `test-backend` compose block. Open a PR linking #190.

---

## 2. Optional polish (no issue — track here; do only if time)
- **T1.4** regression tests per fixed defect · **T1.5** unit-test the hydration fns · **T1.6** fix the `test_runloop.py` basename clash (rename one so the full suite collects in one `pytest`).
- **T2.2/T2.3** move the SOT-forbidden logic out of the sidecar + one source-of-truth for exclusion state · **T2.5** stop `config/policy.yaml` runtime drift.
- **T2.4** tune the monotone `cut` config (+ re-run the monotonicity probe).
- **T3.3** latency-first benchmark · **S6/S7/S8** correlated-failure / open-loop flash-crowd / recover-churn scenarios *(any new harness reuses the `smartload-locust:latest` image — see "Benchmark tooling" — never `pip install` per side)*.
> One prompt for the batch: *"Pick up roadmap item <Txx> from `audit/THESIS_ROADMAP.md` §<n>; read it for scope, branch off `fix/anomaly-overload-exclusion`, keep to its file lane; if it runs/creates a benchmark, use the `smartload-locust:latest` image (build-if-missing) not per-side pip; open a PR."*

---

## 3. The thesis update — this is PHASE 1 [A], the priority (NOT last)
The full, ready-to-paste, chapter-by-chapter prompt is **`audit/THESIS_UPDATE_PROMPT.md`**
(produced from a complete read-through of the current thesis vs the new results). Headline
of that analysis: the thesis currently **CONTRADICTS** the new ground truth — it claims
"the monotone heuristic beats round-robin," is anchored to the **superseded**
`baseline-vs-smartload` tie, and **omits** the decisive exclusion wins + the ablation. The
prompt fixes all of that, in order, quote-anchored.
**Coordinate:** a separate agent has been writing `thesis/`; take one chapter-group at a
time and `git pull` before each so you don't collide. Confirm the central claim is **Track
A** and **RQ1 is scoped to equal capacity** (scaling → Future Work).

---

## 4. File-ownership map (parallel work without conflict)

| Issue | Owns (edit freely) | Must NOT touch |
|---|---|---|
| **[A] thesis update** | `thesis/**` | everything else (pull facts from `audit/*` + `experiments/*`, read-only) |
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

## 5. Execution order at a glance (D2 = equal-capacity)
0. **You:** settle decisions **D1** (Track A) + **D3** (PPO framing). *(D2 ✅; D4/D5 only matter for Phase 3.)*
1. **PHASE 1 — finalize the thesis** *(parallel)*: **[A] thesis update** (`audit/THESIS_UPDATE_PROMPT.md`) ‖ **#181** ‖ **#186** ‖ **#187**. → the thesis is done when [A] lands (it folds in B/C/D).
2. **PHASE 2 — demo-ui**: **merge PR #193** (done). Re-run the demo-ui prompt only if numbers change.
3. **PHASE 3 — Future Work** (none gate the thesis): ‖ **#182**, ‖ **#188**; **#183 → #185**; *(Track B)* **#189 → #190**; optional polish (§2).

*Living doc — update statuses as PRs land. Open issues: #181 #182 #183 #185 #186 #187 #188 #189 #190. Done: #180 (ablation), PR #193 (demo-ui), PR #184/#192 (merged). Detailed thesis prompt: `audit/THESIS_UPDATE_PROMPT.md`.*
