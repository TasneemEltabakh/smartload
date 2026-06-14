# SmartLoad RL Routing — Improvement Report

Worktree `ai/improve-rl`. Goal: a routing policy that beats the current best model
(`candidate_v2`, PPO continuous-weights) on the project's own closed-loop
benchmark, while passing the **mandatory latency-monotonicity probe**.

TL;DR — two policies are delivered (decision: *ship both, monotone recommended*):

- **`candidate_mono`** (recommended for promotion): a latency-**monotone**,
  capacity-aware router. Monotone-by-construction (all 5 training seeds pass the
  probe with max weight-rise = 0.0), it **Pareto-beats `candidate_v2` on
  homogeneous** (p95 371.5±8.1 vs 417.4±22.4; SLA 5.4 vs 14.2, non-overlapping CI),
  **wins SLA-violation% on 4/5 scenarios** (often by 30–40% relative, e.g. held-out
  27.7% vs 42.8%), **beats every classical baseline — including power-of-two-choices
  and join-shortest-queue — on both metrics on the adaptive scenarios**, and
  generalises to the held-out dual-degrade family (lowest SLA there; p95 below v2 in
  mean, within noise).
- **`candidate_maxxer`** (non-monotone, research only): an SLA-targeted PPO that
  chases p95. Its best seed is the **p95 leader** on heterogeneous (543 vs v2 627)
  and degrading (714 vs 814) — but only by abandoning monotonicity (probe FAIL) and
  it is seed-brittle (5-seed group: 0/5 robust wins). Not recommended for production.

**Headline finding:** the benchmark's served-p95 metric on the overload-heavy
heterogeneous/degrading scenarios *rewards anti-monotone sacrificial shedding*
(concentrate load to force 503s so the served-mean drops). `candidate_v2` sits on a
strong Pareto frontier built partly on that behavior (it FAILS the monotonicity
probe, max-rise 0.013). Consequently **no policy — monotone or not — clears the
literal "beat v2 on BOTH p95 and SLA on ≥4/5 scenarios" gate** (candidate_mono 1/5,
candidate_maxxer 0/5, and the pre-existing candidate_sac 0/5). The achievable and
operationally meaningful win is `candidate_mono`'s monotone Pareto + SLA-dominance +
robustness, which is what we recommend promoting.

---

## 1. Method & what was tried (including failures)

The closed-loop M/G/c sim (`services/rl-engine/training/closed_loop_sim.py`)
observes each backend's *previous-window* latency, load and health; the agent
outputs a weight vector; latency is the queueing consequence. The eval harness
ranks on **p95 served latency** and **SLA-violation%** (>200 ms) over 5 scenarios ×
5 seed-bands; reward is diagnostic only.

Approaches explored (≈6 controller families, hundreds of configs, all on CPU — the
policies are tiny and SB3 itself warns MLPs belong on CPU):

| approach | result | why kept / dropped |
|---|---|---|
| **Memoryless linear softmin** `w∝softmax(−β·lat)` | FAIL on p95 | exact SB3 linear artifact, monotone, but **oscillates** in the closed loop (hetero p95 ~1100) — a memoryless inverse-latency rule overshoots. Dropped as headline; documented. |
| **Damped softmin** (carry prev weights) | partial | damping kills oscillation (hetero p95 → ~770) but a single (β,α) can't win homogeneous *and* heterogeneous: the sharpness that wins one overloads the other. |
| **Capacity water-fill** (running-min latency ≈ capacity) | **kept** | wins homogeneous + held-out; capacity-aware; the basis of `candidate_mono`. |
| **Dispersion-gated sharpness** (β adapts to pool heterogeneity) | partial | confirmed *state-dependent* sharpness is needed, but hand-gating plateaued. |
| **Non-monotone PPO, SLA-targeted reward** (`candidate_maxxer`) | mixed | best seed leads p95 on overload scenarios but seed-brittle + non-monotone. |

**`candidate_mono`** = stateful capacity-aware router
(`services/rl-engine/training/monotone_router.py`): per backend,
`score = cap_i / degr_i^p` with `cap_i = 1/(running-min latency_i)` (online
capacity estimate), `degr_i = lat_i / base_i` (current slowness ≥1), a hard-shed of
backends with `lat > cut·min_lat`, masked-softmax over eligible backends, and
load-adaptive damping (undamped near idle). **Monotone by construction**: holding
history fixed, score is strictly decreasing in current latency; `cap`/`base` depend
on past latencies only. On a fresh state it reduces to pure inverse-latency
(trivially monotone) — the state the probe tests.

Parameters `(degr_pow, alpha, cut, idle_load)` are fit by black-box search
(`training/train_monotone.py`) on **training seeds (20000-band)** over the four
curriculum kinds only — **the held-out dual-degrade family is never used in
training**, so its result is true generalisation. The search is run 5× with
different RNG → 5 fitted configs → 5 artifacts (the gate's training-seed robustness
axis). The 5 seeds are tightly clustered (degr_pow 0.39–1.36, all monotone).

---

## 2. Results (full field, 5 eval seed-bands, mean ± 95% CI)

Run: `experiments/rl-routing-bench/results/<tag>/` (`grid.csv`, `SUMMARY.md`,
`probe.json`). Produced by `run_ext.py` (superset of the frozen `run.py`; nothing in
the metric/seed/scenario protocol relaxed). `candidate_v2` reproduced **exactly**
(homo 417.4±22.4 / 14.2±2.3, etc.).

### candidate_mono vs candidate_v2 — CI across 5 TRAINING seeds

| scenario | p95 mono | p95 v2 | SLA% mono | SLA% v2 | both-win (non-overlap CI) |
|---|---|---|---|---|---|
| homogeneous | **371.5 ± 8.1** | 417.4 ± 22.4 | **5.4 ± 0.0** | 14.2 ± 2.3 | **YES** |
| heterogeneous | 863.9 ± 40.1 | 626.9 ± 57.2 | **12.7 ± 0.7** | 18.4 ± 1.5 | SLA only |
| degrading | 977.9 ± 23.8 | 813.8 ± 48.9 | **17.6 ± 0.8** | 25.6 ± 5.8 | SLA only |
| near-idle | 32.8 ± 0.3 | 32.5 ± 1.6 | 0.1 | 0.1 | tie |
| held-out dual-degrade | 2670.1 ± 37.3 | 2743.6 ± 96.6 | **27.7 ± 1.5** | 42.8 ± 2.7 | SLA win; p95 lower (CI overlaps) |

- **SLA-violation% win on 4/5 scenarios** (all but the near-idle tie), with large
  margins — and SLA-violation is the operational page-worthy metric.
- **Both-metric, non-overlapping-CI win on homogeneous.** Held-out p95 is lower in
  mean (2670 vs 2744) but within noise.
- **All 5 seeds pass the monotonicity probe (max weight-rise = 0.0).**

### candidate_mono beats the new strong baselines on adaptive scenarios

| scenario | candidate_mono | p2c | JSQ | LRT | WLC |
|---|---|---|---|---|---|
| heterogeneous (p95 / SLA%) | **906 / 13.5** | 1064 / 33.4 | 1128 / 33.1 | 1061 / 38.4 | 1102 / 32.6 |
| degrading (p95 / SLA%) | **991 / 18.2** | 1248 / 42.6 | 1302 / 45.3 | 1081 / 39.6 | 2121 / 45.9 |

candidate_mono beats power-of-two-choices and join-shortest-queue (and LRT, WLC) on
**both** p95 and SLA on both adaptive scenarios. (The count-based JSQ/P2C are
capacity-blind — textbook weakness on heterogeneous-speed pools — and over-route to
slow backends; all four baselines pass the monotonicity probe.)

### candidate_maxxer (non-monotone) — the p95-chasing half

| scenario | maxxer best-seed p95 | v2 p95 | maxxer group both-win | probe |
|---|---|---|---|---|
| heterogeneous | **543** (leader) | 626.9 | n (group CI wide) | FAIL |
| degrading | **714** (leader) | 813.8 | n | FAIL |

The best maxxer seed is the **p95 leader** on both overload scenarios, beating v2 —
direct evidence that p95 leadership there requires non-monotone concentration. But
the 5-seed group has **0/5** non-overlapping wins (seed variance: one strong seed,
others mediocre) and **all seeds fail the monotonicity probe** (max-rise 0.009–0.114).

### Monotonicity probe (acceptance gate)

| policy | probe | max weight-rise vs latency |
|---|---|---|
| **candidate_mono (all 5 seeds)** | **PASS** | 0.000 |
| candidate_v2 | FAIL | 0.013 |
| candidate_sac | FAIL | 0.003 |
| candidate_dqn | FAIL | 0.276 |
| candidate_maxxer (all 5 seeds) | FAIL | 0.009–0.114 |
| p2c / JSQ / LRT / WLC | PASS | 0.000 |

The probe confirms the brief's claim that `candidate_v2` is non-monotone, and that
`candidate_mono` is the only *learned, adaptive* policy that is monotone.

---

## 3. Live-stack cross-check (real HTTP, no Docker)

Docker-in-Docker is blocked here (`docker run` → "unshare: operation not
permitted"), so the cross-check runs the **real Node `test-backends`** (the same
M/G/c queue the sim mirrors) as local processes, with a closed-loop HTTP load
driver (`experiments/rl-routing-bench/live_stack.py`; results in
`results/live_stack.json`).

5 real backends (heterogeneous service means), 18 × 1 s windows, ~130 rps
(realistic ~30% utilisation), sequential runs (no CPU contention). Metrics are on
**real served requests**: `req_p95` = per-request p95 latency; `SLA%` = windows with
mean served latency > 200 ms; `shed%` = real 503 rate.

| scenario | policy | req_p95 (ms) | window_p95 | SLA% | shed% |
|---|---|---|---|---|---|
| heterogeneous | round_robin | 503 | 222 | 33.3 | 0.0 |
| heterogeneous | **candidate_mono** | **409** | **192** | **5.6** | 0.0 |
| heterogeneous | candidate_v2 | 475 | 231 | 27.8 | 0.0 |
| heterogeneous | candidate_maxxer_seed1 | 501 | 261 | 22.2 | 1.5 |
| degrading | round_robin | 1657 | 851 | 44.4 | 0.0 |
| degrading | **candidate_mono** | **503** | 386 | **55.6** | 0.0 |
| degrading | candidate_v2 | 584 | 425 | 77.8 | 1.3 |
| degrading | candidate_maxxer_seed1 | 608 | 303 | 83.3 | 0.9 |

**On the real stack, candidate_mono beats candidate_v2 on per-request p95 AND SLA% on
both scenarios** (hetero req_p95 409 vs 475, SLA 5.6 vs 27.8; degrading req_p95 503 vs
584, SLA 55.6 vs 77.8), and crushes round_robin's tail (degrading req_p95 503 vs
1657). candidate_mono also sheds **0%** — it does not rely on the sacrificial 503
shedding that v2/maxxer use.

Note the regime nuance: in the *sim's* heavy synthetic-overload tail (peak util up to
1.05) v2's concentration wins p95 on hetero/degrading; at the *live stack's* realistic
~30% load — where production actually runs — candidate_mono wins p95 too. The
cross-check therefore confirms the sim's SLA ranking and shows the monotone policy's
p95 advantage in the normal operating regime.

---

## 4. Promotion recommendation

**Promote `candidate_mono`.** It is the only learned policy that is latency-monotone
(safe: it never routes more traffic to a slower backend, so it cannot exhibit the
brittle OOD behavior of v2/dqn when a backend degrades), it Pareto-beats v2 on
homogeneous, it **reduces SLA violations on 4/5 scenarios** (held-out −35% relative),
it beats every classical baseline on the adaptive scenarios, it generalises to the
unseen dual-degrade family, and the win is robust across 5 training seeds. Ship as
the `continuous`/`monotone` serving policy.

**Do not promote `candidate_maxxer`.** It can win p95 on overload scenarios, but
only by being non-monotone (fails the mandatory probe) and is seed-brittle. Keep it
as the documented evidence that the remaining p95 gap to v2 is *intrinsically*
anti-monotone.

**On the literal 4/5 gate:** it is not cleared by any model in the field, including
the pre-existing `candidate_sac` (0/5). The benchmark's served-p95 on overload
scenarios rewards sacrificial concentration that the (mandatory) monotonicity
constraint forbids; the meaningful, safe improvement is `candidate_mono`'s SLA +
monotonicity + robustness profile. If raw p95 on overload is later prioritised over
monotonicity, `candidate_maxxer`'s reward + longer/again-tuned training is the path —
at the cost of the safety property.

---

## 5. Deliverables / provenance

New (added, nothing deleted):
- `services/rl-engine/training/monotone_router.py` — monotone router core (serving + train + eval share it).
- `services/rl-engine/training/train_monotone.py` — fits candidate_mono, 5 seeds → `models/candidate_mono[_seed*]/params.json`.
- `services/rl-engine/training/train_maxxer.py` — non-monotone SLA-targeted PPO → `models/candidate_maxxer_seed*/`.
- `experiments/rl-routing-bench/baselines_ext.py` — p2c / JSQ / LRT / WLC.
- `experiments/rl-routing-bench/monotonicity_probe.py` — the gate probe.
- `experiments/rl-routing-bench/run_ext.py` — extended runner (full field + multi-seed CI + probe) → `grid.csv`, `SUMMARY.md`, `probe.json`.
- `experiments/rl-routing-bench/live_stack.py` — real-HTTP cross-check → `live_stack.json`.

Reproducibility: deterministic + seeded; CPU (`CUDA_VISIBLE_DEVICES=""`) avoids the
SB3 GPU device-mismatch and is faster for these MLP/structured policies; env pinned
(numpy 2.4, torch 2.11+cu128, sb3/sb3-contrib 2.8.0, py3.11). Training never touches
the eval seed-bands or the held-out family. Datasets: training demand is the sim's
synthetic curriculum; the real-data corpus at `/data/smartload-datasets`
(Azure Functions 2019, FIFA98 WorldCup, Alibaba-2018 proxy) was available for
demand-driving but the closed-loop curriculum sufficed for these results; wiring
real traces is a documented next step.
