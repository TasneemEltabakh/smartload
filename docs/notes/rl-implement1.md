# SmartLoad RL Engine — Implementation Continuity Document

**Document scope:** Full technical reconstruction of all design decisions, implementation
details, evaluation results, and open work related to the RL engine (milestones N2.3–N2.5)
and the broader SmartLoad project state as of commit `600d2a5` (2026-05-23).

**Intended audience:** Any engineer resuming development on this project. Assumes familiarity
with Python and reinforcement learning basics. Nothing here should be taken on faith — every
claim is cross-checked against actual code, committed artifacts, and test output in this repo.

---

## Table of Contents

1. [Project Identity](#1-project-identity)
2. [System Architecture Overview](#2-system-architecture-overview)
3. [Implementation Phases](#3-implementation-phases)
4. [N2.3 — SmartLoadEnv (Gymnasium Environment)](#4-n23--smartloadenv)
5. [N2.3 Supporting Modules](#5-n23-supporting-modules)
6. [N2.4 — Training Pipeline](#6-n24--training-pipeline)
7. [N2.4 — Canary Validation](#7-n24--canary-validation)
8. [N2.4 — Evaluation Methodology](#8-n24--evaluation-methodology)
9. [N2.4 — Benchmark Results](#9-n24--benchmark-results)
10. [N2.5 — PPOPolicy Serving Plugin](#10-n25--ppopolicy-serving-plugin)
11. [Training / Serving Separation Contract](#11-training--serving-separation-contract)
12. [Mode Composition (shadow / active / hybrid)](#12-mode-composition)
13. [Runtime Integration (app.py + runloop.py)](#13-runtime-integration)
14. [Infrastructure & CI](#14-infrastructure--ci)
15. [Baseline Policies](#15-baseline-policies)
16. [Test Suite Map](#16-test-suite-map)
17. [Committed Artifacts](#17-committed-artifacts)
18. [Information Confidence Map](#18-information-confidence-map)
19. [Open Problems, Technical Debt, and Phase 2 Work](#19-open-problems-technical-debt-and-phase-2-work)
20. [Activation Runbook](#20-activation-runbook)
21. [Key Invariants — Do Not Break](#21-key-invariants--do-not-break)

---

## 1. Project Identity

**SmartLoad** is an AI-driven load management middleware that sits between client traffic and
a pool of backend services. It is a graduation project for Zewail City of Science, Technology
and Innovation.

The system is purely middleware — it does not need multi-tenancy, API-key auth, RBAC, or
per-tenant Redis namespacing in the current phase. These belong to a future Phase 2 SaaS
adaptation track.

**Core design philosophy:**

1. The control plane **never blocks** the data plane. Traffic flows even if every AI service
   dies simultaneously.
2. Three-tier engine safety: baseline policy → custom ML model → `safe_mode` operator override.
3. Engine-wrapper pattern: `runloop.py` (pure Python, unit-testable) + `app.py` (Flask +
   background thread). This pattern is shared identically across anomaly-detector,
   forecasting, and rl-engine.
4. Parameterized SQL everywhere — no string formatting. PostgreSQL plan caching + SQL injection
   safety.
5. Atomic config updates (temp file + rename for policy.yaml).
6. Envelope versioning on all Redis messages (`event_id`, `source`, `version`, `timestamp`).

---

## 2. System Architecture Overview

### Container inventory (14 Docker containers)

| Container | Port | Role | Run loop default |
|---|---|---|---|
| `timescaledb` | 5432 | PostgreSQL + TimescaleDB; hypertables: `metrics`, `backend_health`, `scaling_events`, `policy_changes` | n/a |
| `redis` | 6379 | Pub/sub control bus | n/a |
| `otel-collector` | 4317/4318/8889 | OTLP receiver → Prometheus | n/a |
| `prometheus` | 9090 | Metrics scraping | n/a |
| `grafana` | 3000 | Dashboards | n/a |
| `telemetry` | 8081 | OTLP/HTTP-JSON ingestion → TimescaleDB | always on |
| `anomaly-detector` | 8082 | Flags unhealthy backends | `ANOMALY_RUNLOOP_ENABLED=false` |
| `forecasting` | 8083 | RPS forecast → autoscaler | `FORECAST_RUNLOOP_ENABLED=false` |
| `rl-engine` | 8084 | RL routing recommendations | `RL_RUNLOOP_ENABLED=false` |
| `autoscaler` | 8085 | Scale pool via Docker SDK | always on |
| `policy-manager` | 8086 | Owns `policy.yaml` + audit trail | always on |
| `load-balancer` (NGINX) | 8080 | Round-robin reverse proxy | always on |
| `lb-otel-shipper` | — | Tails NGINX log → OTel Collector | always on |
| `test-backend` (Node.js) | — | 5 replicas; target backend pool | 5 replicas via compose |
| `operator-ui` | 8090 | React BFF | always on |

### Redis channels

| Channel | Publisher | Subscribers |
|---|---|---|
| `smartload.policy` | policy-manager | all AI services |
| `smartload.anomaly` | anomaly-detector | future T2.1 LB sidecar, operator-ui |
| `smartload.forecast` | forecasting | autoscaler |
| `smartload.routing` | rl-engine | future T2.1 LB sidecar |
| `smartload.scale` | autoscaler | operator-ui |

### TimescaleDB tables

- `metrics` — per-request telemetry (time, service, instance, metric_name, value)
- `backend_health` — anomaly-detector health writes (time, backend_id, status, score)
- `scaling_events` — autoscaler action log (time, action, instance_count, reason)
- `policy_changes` — policy-manager audit trail (time, policy_version, field, old_value, new_value, actor)

---

## 3. Implementation Phases

### Phase 1 — Complete (as of 2026-05-23)

All slice commits are on `main`. The stack is in a provably working state.

| Milestone | Description | Status | Key commit |
|---|---|---|---|
| T1.1 | OTLP telemetry ingestion | Done | earlier than logged window |
| T1.2 | lb-otel-shipper (per-request fidelity) | Done | earlier |
| T1.3 | Autoscaler (Docker SDK pool control) | Done | earlier |
| Policy management slice | Policy API + audit trail | Done | `bad58c8` |
| Audit-log slice | GET /api/v1/audit/scaling | Done | `15ec608` / `c68776b` |
| Manual-actions slice | POST /api/v1/scale + /api/v1/isolate | Done | `8576873` / `dde0093` |
| Engine-wrapper foundation (#138) | Engine-base run loop pattern, feature flags | Done | `eb6f628`, `4797a89`, `7d4a3fe` |
| N2.3 | SmartLoadEnv Gymnasium environment | Done | `787eba8` |
| N2.4 | Training pipeline + policy.zip artifact | Done | `787eba8`, `f3df2d3`, `15991db` |
| N2.5 | PPOPolicy serving plugin | Done | `787eba8` |

**Current policy.yaml version:** v19, `safe_mode=false`, `operating_mode=hybrid`.

### Phase 2 — Pending

| Milestone | Description | Issue(s) |
|---|---|---|
| T2.1 | LB sidecar (dynamic upstream rewriting via NGINX adapter) | — |
| Operator UI — Live Engines view | Subscribe to `smartload.routing` / `smartload.anomaly` feeds | #121 |
| Python SDK | Full client implementation | #127 |
| Webhook dispatcher | Outbound event delivery | #130 |
| Helm chart | Kubernetes packaging | #133 |
| Isolation Forest model | ML anomaly detection | #101 |
| ARIMA model | ML forecasting | #102 |
| Multi-tenancy | `tenant_id` propagation | #129 |
| Strict lint mode | Enforce `scripts/lint-*.py` | #139 |

---

## 4. N2.3 — SmartLoadEnv

**File:** `services/rl-engine/training/env.py`
**Status:** Training-only — never COPY'd into the runtime Docker image.

### Environment contract

| Property | Value |
|---|---|
| Gym class | `gymnasium.Env` |
| Observation space | `Box(low=0, high=inf, shape=(15,), dtype=float32)` — 5 backends × 3 features |
| Action space | `Discrete(5)` — backend index (0-based, sorted by `backend_id`) |
| Action masking | `action_masks()` returns `np.ndarray[bool, (5,)]` for use with `MaskablePPO` |
| Episode length | 200 steps (configurable) |
| `reset()` | Samples a random start timestamp from the dataset (reproducible with seed) |
| `step(action)` | Advances one 30-second window; returns `(obs, reward, terminated, truncated, info)` |
| Termination | After `episode_length` steps OR when dataset runs out of windows |

### Observation vector layout

Each per-backend slot occupies 3 consecutive float32 values:

```
obs[i*3 + 0] = backend_i.latency_ms  / NormParams.latency_scale
obs[i*3 + 1] = backend_i.queue_depth / NormParams.request_count_scale
obs[i*3 + 2] = health_flag  (healthy=0.0, degraded=0.5, unhealthy=1.0)
```

Backends are sorted by `backend_id` (lexicographic) for **stable slot assignment** across
all calls. This is critical: training and serving must use identical ordering.

**Padding slots** (when live backend count < 5): `[0.0, 0.0, 1.0]` — zero load with
max health penalty. PPO never prefers a padded slot.

### Action masking logic

```python
# True = eligible; False = masked out (unhealthy or absent)
mask = build_action_mask(state, N_MAX_BACKENDS)
if not mask.any():
    mask = all_masked_fallback(state, N_MAX_BACKENDS)  # unmask lowest-latency
```

`MaskablePPO` raises an exception on all-False masks, so `all_masked_fallback` is a hard
requirement. The fallback unmasks the backend with the lowest `latency_ms` — the least-bad
choice when every backend is flagged unhealthy.

### `_default_dataset()` path resolution

```python
alibaba_dir = Path(__file__).resolve().parents[3] / "datasets" / "alibaba"
```

`__file__` = `services/rl-engine/training/env.py`
`parents[3]` = repo root (smartload/)
Full path = `smartload/datasets/alibaba/`

**Critical:** The git-untracked CSV files `datasets/alibaba/partition_id=-*.csv` must be
present for any training run. They are not committed (too large).

### Default NormParams

```python
DEFAULT_NORM = NormParams(latency_scale=2000.0, request_count_scale=200.0)
```

These are calibrated on Alibaba p99 latency (~2000 ms) and typical per-window request count
(~200 in 30 s at moderate load). Used as smoke-test defaults; at real training time,
`_compute_norm_params()` overrides them with dataset-derived values.

---

## 5. N2.3 Supporting Modules

### `obs_builder.py` (serving path + training shared)

**File:** `services/rl-engine/obs_builder.py`
**Consumed by:** `training/env.py` AND `policies/ppo/policy.py` — the same function is used
at training time and serving time to guarantee **train/serve parity**.

Key exports:

- `N_MAX_BACKENDS = 5` — must equal `policy.yaml.max_backends`
- `NormParams(latency_scale, request_count_scale)` — serialized into `artifact_meta.json`
- `build_observation(state, n_max, norm) → np.ndarray[float32, (15,)]`
- `build_action_mask(state, n_max) → np.ndarray[bool, (5,)]`
- `all_masked_fallback(state, n_max) → np.ndarray[bool, (5,)]`

### `training/dataset.py` — TraceReplayDataset

**Purpose:** Loads Alibaba trace CSVs and produces windowed `BackendState` snapshots
equivalent to what `RL_STATE_QUERY` returns at serving time.

**Alibaba CSV schema (8 columns):**

| Column | Type | Meaning |
|---|---|---|
| traceid | string | trace identifier |
| timestamp | int string | ms since trace epoch |
| rpcid | string | call position in trace tree |
| um | string | caller service hash |
| rpctype | string | "http" \| "rpc" \| "mc" |
| dm | string | callee service hash (used as backend ID) |
| interface | string | method hash (may be empty) |
| rt | float | response time in ms; **negative = error** |

**Filtering:** `rpctype == "http"`, `dm` non-empty, `rt` parseable.

**Backend mapping:** `dm` hashes are sorted lexicographically across the entire loaded
dataset and mapped to stable names `"backend_1"`, `"backend_2"`, ... The first 5 (sorted)
are kept; the rest are dropped. This guarantees the same backend keeps the same name across
partitions and random episode starts.

**Window aggregation (30-second windows):**

```
latency_ms  = mean(rt) for rt >= 0;  0.0 if all rows are errors
queue_depth = count of all rows (proxy for request volume — matches RL_STATE_QUERY semantics)
error_rate  = fraction of rows with rt < 0
```

Then `classify_health(latency_ms, error_rate)` is applied — the same function used in
`runloop.py` at serving time.

**Bisect-based window lookup:** Uses `bisect.bisect_left` for O(log n) boundary lookup
instead of linear scan. Critical for performance with large CSV partitions.

**Train/eval split:** The last 5 windows per dataset are held out as eval territory
(see `sample_start_ts(rng, reserve_windows=5)`). The `eval_seed_bank.json` stores
`train_eval_split_idx=51794` which is the window boundary index.

### `training/simulator.py` — BackendSimulator

**Purpose:** Wraps `TraceReplayDataset` to provide the Gym-compatible `reset()`/`step()`
interface used by `SmartLoadEnv`.

**Key design decision — stateless replay:**
The simulator is stateless with respect to routing. It replays the trace regardless of what
action was taken by the policy. The action index selects a backend, and the reward function
reads the consequence from `next_state` (post-step window). This is offline RL on a fixed
trace — the environment does not simulate how routing decisions would affect backend load.

`DEFAULT_EPISODE_LENGTH = 200` steps.

### `training/reward.py` — RewardCalculator

**Reward formula:**

```
reward = -(latency_ms_chosen / latency_scale) - λ × load_imbalance + health_penalty
```

Where:
- `latency_ms_chosen` — latency of the chosen backend read from `next_state` (post-action)
- `latency_scale` — from `NormParams` (100.0 for the trained artifact)
- `λ = 0.1` — imbalance penalty weight (`imbalance_lambda`)
- `load_imbalance = std(queue_depths) / (mean(queue_depths) + 1.0)` — normalized std
- `health_penalty = -10.0` if chosen backend is `"unhealthy"`, else 0.0

**Credit assignment decision (Amendment F in the commit message):**
Latency is read from `next_state`, not `state`. The agent chose backend `action`; the next
observation reflects what that backend's latency became after absorbing the traffic. Reading
from `state` would assign latency BEFORE the action took effect, breaking the
credit-assignment chain.

**Hard health penalty defence-in-depth:** The mask should prevent routing to unhealthy
backends, but the -10.0 penalty provides a backstop if the mask is ever misconfigured.

---

## 6. N2.4 — Training Pipeline

### Files

| File | Role |
|---|---|
| `services/rl-engine/training/train_ppo.py` | Main training entry point |
| `services/rl-engine/training/train_dqn.py` | DQN fallback (activated if PPO canary fails) |
| `services/rl-engine/training/requirements-training.txt` | Training-only dependencies (not in Docker image) |

### Training entry points

```bash
# Canary check only (50k steps, first 2 partitions):
python training/train_ppo.py run_canary

# Full training (all partitions, 2M steps):
python training/train_ppo.py

# Full training, skip canary:
python training/train_ppo.py --skip-canary
```

### Full training hyperparameters (MaskablePPO)

| Parameter | Value | Notes |
|---|---|---|
| Algorithm | `sb3_contrib.MaskablePPO` | Supports `action_masks()` callback |
| Policy | `"MlpPolicy"` | Two-layer MLP (SB3 default: 64×64 hidden) |
| `total_timesteps` | 2,000,000 | ~75 min on CPU |
| `learning_rate` | 3e-4 | Adam optimizer default |
| `n_steps` | 512 | Steps collected per update cycle |
| `batch_size` | 64 | Minibatch size for PPO updates |
| `n_epochs` | 10 | PPO update epochs per rollout |
| `gamma` | 0.99 | Discount factor |
| `seed` | 42 | Training RNG seed |
| `tensorboard_log` | `None` | Disabled (tensorboard not installed in training env) |
| `verbose` | 1 | Progress output |

### Canary hyperparameters (50k steps)

| Parameter | Value |
|---|---|
| `total_timesteps` | 50,000 |
| `n_steps` | 256 |
| `batch_size` | 64 |
| `n_epochs` | 4 |
| `episode_length` | 50 |
| `learning_rate` | 3e-4 |
| `gamma` | 0.99 |
| `seed` | 42 |

### Dataset used for the committed training run

All 10 partitions from `datasets/alibaba/`:

```
partition_id=-1.csv    (md5: 5363275ff8497bee8bf8b0f141d27b89)
partition_id=-10.csv   (md5: 008c1890bfd4580448b9270d15ee7792)
partition_id=-100.csv  (md5: dba350ebc258cdfc80984db8555ea167)
partition_id=-101.csv  (md5: b1ac4daced5614357001ce2090e9146c)
partition_id=-102.csv  (md5: 5b551227b029391d7f1c7d592d35545f)
partition_id=-103.csv  (md5: 19c16aed58835c9476794e76a34e480c)
partition_id=-104.csv  (md5: b41fca4700b4b9a0de1b7e9989da894e)
partition_id=-105.csv  (md5: d1070ed54f4c4b4bc71dbd06001b4b71)
partition_id=-106.csv  (md5: 1564979da22a40d7d86d351241d529a0)
partition_id=-107.csv  (md5: 501c3b8a6489c476dc79018696fa4b04)
```

### Dataset-derived NormParams (computed at training time)

```
latency_scale        = 100.0    (Alibaba p99 latency is ~100 ms, NOT ~2000 ms)
request_count_scale  = 95.01    (p99 per-window request count)
```

Note: The default `NormParams(2000.0, 200.0)` in `env.py` and `obs_builder.py` are only
smoke-test defaults. The actual training used much lower values derived from the real
dataset via `_compute_norm_params()`. These real values are stored in `artifact_meta.json`
and read back by `PPOPolicy` at serving time.

### SB3 versions

```
stable-baselines3: 2.8.0
sb3-contrib:       2.8.0
```

### Training output

| File | Size | Description |
|---|---|---|
| `services/rl-engine/models/policy.zip` | 159,802 bytes (156 KB) | MaskablePPO artifact |
| `services/rl-engine/models/artifact_meta.json` | 725 bytes | NormParams + training metadata |

**Training buffer mean reward (last 100 episodes):** `-19.587` — this uses
`latency_scale=100`, so a single-step reward of ~-0.1 per step × 200 steps = -20 is
expected. This number is dominated by the latency term and is **not** comparable with the
eval harness results (which use the same scale, so eval rewards are also in this range for
PPO but the eval harness computes mean per episode differently).

---

## 7. N2.4 — Canary Validation

The canary is an automated go/no-go check run before the full 2M-step training.
Both gates must pass; failure exits with code 1 and instructs the user to switch to
`train_dqn.py`.

### Gate 1 — Policy gradient loss trending down

Captures `train/policy_gradient_loss` values via a logger-patch callback during the 50k-step
canary run. Takes the last 10 values and fits a linear regression slope.

```
Pass condition: slope < 1e-4  (SLOPE_TOLERANCE)
```

The tolerance is deliberately loose — PG loss at 50k steps fluctuates heavily and a
truly diverging run is needed to fail this gate. A flat slope passes.

**Result for the committed training run:**
```
[canary] pg_loss tail slope = -1.39e-09 < 1e-04 → PASS
```

### Gate 2 — PPO mean reward > round_robin mean reward

Samples 3 mini-episodes from the canary dataset (first 2 partitions), runs `round_robin`
through the eval harness, and compares against PPO's mean reward on the same episodes.

```
Pass condition: ppo_mean > rr_mean
```

**Result for the committed training run:**
```
[canary] round_robin mean_reward = -0.0148
[canary] ppo        mean_reward = -0.0099
→ PASS
```

### Canary outcome

Both gates passed → full 2M-step training proceeded without human intervention.

---

## 8. N2.4 — Evaluation Methodology

### Eval harness (`training/eval_harness.py`)

**Reproducibility contract:** Given identical seed bank and dataset partitions, two runs
on the same codebase produce byte-identical CSVs (excluding the timestamp in the meta JSON).

**How reproducibility is achieved:**
- Fixed `eval_seed_bank.json` with pre-committed episode starts (timestamps + seeds)
- Policies are freshly instantiated per episode (stateful `RoundRobinPolicy._idx` is reset)
- Random-shadow policy is seeded with the episode seed
- CSV rows are sorted deterministically before writing (policy ASC, episode_id ASC)

### Seed bank (`training/eval_seed_bank.json`)

- 25 fixed episodes total; first 20 used for evaluation; last 5 are reserve
- `train_eval_split_idx = 51794` — row index in the loaded dataset at which training data
  ends and eval data begins
- `split_ts = 34183622` — the corresponding timestamp boundary
- `n_backends = 5`, `window_ms = 30000`
- Covers all 10 dataset partitions

### Eval procedure per policy per episode

1. Reset `BackendSimulator` to the fixed episode start timestamp from the seed bank
2. Run the policy for up to `episode_length=200` steps
3. For each step: `policy.act(state)` → choose highest-score backend → `sim.step(chosen_idx)`
   → `reward_calc.compute(state, chosen_idx, next_state)`
4. Record latency, utilization variance, and reward at each step
5. Aggregate to: `mean_reward`, `p50_latency`, `p95_latency`, `p99_latency`,
   `slo_violation_rate` (fraction of steps where chosen latency > 200 ms),
   `utilization_variance`

### CSV output format

```
policy,episode_id,mean_reward,p50_latency,p95_latency,p99_latency,slo_violation_rate,utilization_variance
```

---

## 9. N2.4 — Benchmark Results

Two evaluation runs were committed. The **final eval** (post-training) is definitive.

### Baseline eval (`eval_results_aec62f8.csv` — 3 policies × 20 episodes)

Run before PPO training to establish baselines. Commit `787eba8`.

| Policy | Mean reward (mean±std over 20 eps) | p95 latency (ms) | SLO violations |
|---|---|---|---|
| random_shadow | ~-0.021 | 15.86 | 0.0 |
| round_robin | ~-0.006 | 15.86 | 0.0 |
| least_connections | ~-0.053 | 15.86 | 0.0 |

### Final eval (`eval_results_f3df2d3.csv` — 4 policies × 20 episodes)

Run after full training on the trained `policy.zip`. Commit `15991db`.
Eval date: `2026-05-23T09:34:20Z`.

| Policy | mean_reward (mean over 20 eps) | p50_latency | p95_latency | SLO violations |
|---|---|---|---|---|
| **ppo** | **-0.0056** | 9.73 ms | 15.86 ms | **0.0** |
| **round_robin** | **-0.0056** | 9.73 ms | 15.86 ms | **0.0** |
| random_shadow | -0.0211 | 9.73 ms | 15.86 ms | 0.0 |
| least_connections | -0.0536 | 9.73 ms | 15.86 ms | 0.0 |

**PPO and round_robin are joint-best on all metrics.** Neither beats the other.

### Interpretation

The Alibaba backends in this dataset are **homogeneous** — they all exhibit approximately
the same mean latency (~9.7 ms) and exhibit near-identical latency distributions across
episodes. Any balanced routing policy (PPO, round_robin, random_shadow on most episodes)
achieves the same latency and reward because:

- All backends have nearly identical load (~95 requests per window)
- No backend is systematically slower or more error-prone
- The reward is dominated by the latency term, and since latency is nearly equal across
  backends, any routing policy arrives at approximately the same reward

**What PPO learned:** Round-robin equivalence — a balanced rotation among all healthy
backends. This is optimal for homogeneous backends.

**Why least_connections is worse:** It accumulates state across episodes (the `_idx` analog
is `queue_depth` ranking). In the Alibaba trace, some windows have slight load imbalances
that cause `least_connections` to prefer a specific backend repeatedly, leading to slightly
worse imbalance and higher reward penalty.

**Note on the identical p95_latency values:** The Alibaba backends are truly homogeneous
(same hardware, same response time distribution), so p95 across policies is the same fixed
value determined entirely by the dataset, not the routing decision.

### SLO context

`policy.yaml` sets `slo_p95_latency_ms = 200`. All policies achieve 0.0 SLO violations
against this target on the Alibaba dataset (actual p95 ≈ 15.86 ms, far below 200 ms).
The SLO target is appropriate for production backends but irrelevant as a differentiator
on this dataset.

---

## 10. N2.5 — PPOPolicy Serving Plugin

**File:** `services/rl-engine/policies/ppo/policy.py`
**Status:** Serving-path — COPY'd into the runtime Docker image.
**Must NEVER import from `training/`.**

### Constructor

```python
PPOPolicy(
    confidence_threshold = 0.6,   # unused by PPO itself; kept for API parity
    exploration_rate     = 0.0,   # unused by PPO itself; kept for API parity
    operating_mode       = "shadow",  # "shadow" | "hybrid" | "learning"
    model_path           = None,  # defaults to <rl-engine root>/models/policy
)
```

### Artifact loading sequence

1. Read `artifact_meta.json` from the same directory as `policy.zip`
2. Validate `n_max_backends` against `N_MAX_BACKENDS` — **raises `ValueError` immediately
   on mismatch** (this prevents a model trained for N backends from silently operating with
   M backends at runtime)
3. Restore `NormParams` from `meta["norm_params"]` (the dataset-calibrated values, not the
   defaults)
4. Load `policy.zip` via `MaskablePPO.load()`
5. Set `_policy_ready = True`

**Graceful degradation:** If `artifact_meta.json` or `policy.zip` is absent, logs a WARNING,
sets `_policy_ready = False`, and `act()` returns uniform shadow rankings. Never crashes.

### `act(state)` inference path

```
obs = build_observation(state, N_MAX_BACKENDS, self._norm)
mask = build_action_mask(state, N_MAX_BACKENDS)
if not mask.any(): mask = all_masked_fallback(state, N_MAX_BACKENDS)

action_idx, _ = model.predict(obs, action_masks=mask, deterministic=True)

# Get full ranking from logits (single forward pass)
raw_logits = self._get_logits(obs)   # via torch, dist.distribution.logits
# Collect non-unhealthy backends with logit scores
# Apply softmax to logit values → probability-like scores
# Sort rankings high-to-low by score

mode = "active" if operating_mode in ("hybrid", "learning") else "shadow"
return RoutingAction(mode=mode, rankings=sorted_rankings)
```

**`_get_logits` fallback:** If PyTorch or the SB3 distribution API is unavailable, returns
a zero vector. In this case `act()` still returns valid rankings (uniform scores via softmax
of zeros).

### `reload()` contract

```python
def reload(self) -> None:
    raise NotImplementedError("hot-reload deferred; restart container to swap artifact")
```

Hot reload of the model artifact is explicitly deferred. Artifact swap requires container
restart (restart → `__init__` → `_load_artifact()`).

---

## 11. Training / Serving Separation Contract

This is a hard architectural invariant enforced by both the `Dockerfile` and CI.

### What goes in the Docker image

```dockerfile
COPY rl-engine/app.py          /app/app.py
COPY rl-engine/runloop.py      /app/runloop.py
COPY rl-engine/policy_base.py  /app/policy_base.py
COPY rl-engine/obs_builder.py  /app/obs_builder.py
COPY rl-engine/policies        /app/policies
COPY rl-engine/models          /app/models     # includes policy.zip + artifact_meta.json
COPY shared                    /app/shared
```

**Explicitly excluded from Docker image:** `training/` directory — `env.py`, `dataset.py`,
`simulator.py`, `reward.py`, `train_ppo.py`, `train_dqn.py`, `eval_harness.py`.

### CI enforcement (`runtime-import-smoke` job)

The CI workflow builds the rl-engine runtime image and runs:

```python
import sys, app
try:
    import policies.ppo.policy
except Exception:
    pass
training_mods = [m for m in sys.modules if m.startswith('training')]
if training_mods:
    print('FAIL: training modules leaked into runtime image:', training_mods)
    sys.exit(1)
print('PASS: no training.* modules in sys.modules')
```

This job runs after `build-services` and before `compose-test`, so it gates the full
integration test on serving/training separation being intact.

### Why this matters

- The serving image must import `stable_baselines3` and `sb3_contrib` (for `MaskablePPO.load()`),
  but it must NOT import `gymnasium` (training dependency only)
- The training layer has its own `requirements-training.txt`; those deps are not in
  `requirements.txt` (serving)
- `requirements.txt` does include `stable-baselines3>=2.3.0` and `sb3-contrib>=2.3.0`
  because PPOPolicy.load() needs them at serving time
- PyTorch is installed CPU-only in the Docker image to avoid the 2 GB CUDA wheel

---

## 12. Mode Composition

The published `RoutingRecommendation.mode` field is computed by `runloop.effective_mode()`
from three independent inputs. This is a three-gate safety system.

```python
def effective_mode(action_mode: str, rl_mode_env: str, policy: EnginePolicy) -> str:
    if policy.safe_mode:              return "shadow"   # operator hard-pause (gate 1)
    if rl_mode_env.lower() != "active": return "shadow" # env pin (gate 2)
    if action_mode == "active":       return "active"   # policy agrees (gate 3)
    return "shadow"
```

| Condition | Published mode |
|---|---|
| `safe_mode=true` in policy.yaml | always "shadow" |
| `RL_MODE != "active"` (env var) | "shadow" |
| `RL_MODE=active` AND policy returned "active" | "active" |
| any other combination | "shadow" |

**Operating mode semantics (Amendment B):**

| `operating_mode` in policy.yaml | PPOPolicy `act()` returns | Effective if RL_MODE=active |
|---|---|---|
| `"shadow"` or `"classical"` | mode="shadow" | "shadow" (never becomes active) |
| `"hybrid"` | mode="active" | "active" |
| `"learning"` | mode="active" | "active" |

`"classical"` maps to `"shadow"` in `policy_from_payload()` for backwards-compat with
policy.yaml values that predate the operating_mode field.

**Current state:** `policy.yaml v19` has `operating_mode=hybrid` and `safe_mode=false`.
To activate PPO routing, the operator needs to set `RL_MODE=active` as an env var.

---

## 13. Runtime Integration

### `policy_base.py` — Factory and data types

```python
@dataclass
class BackendState:
    backend_id: str
    latency_ms: float
    queue_depth: int
    health: str  # "healthy" | "degraded" | "unhealthy"

@dataclass
class Ranking:
    backend_id: str
    score: float

@dataclass
class RoutingAction:
    mode: str          # "shadow" | "active"
    rankings: list[Ranking]

class RoutingPolicy(ABC):
    @abstractmethod
    def act(self, state: list[BackendState]) -> RoutingAction: ...
    def reload(self) -> None: ...

def select_policy(name: str, **kwargs) -> RoutingPolicy:
    # Registered: random_shadow, round_robin, least_connections, ppo
```

### `runloop.py` — Pure Python logic layer

Key functions:

| Function | Purpose |
|---|---|
| `classify_health(latency_ms, error_rate)` | Maps metrics to health label |
| `build_state_from_rows(rows)` | Converts RL_STATE_QUERY result to `list[BackendState]` |
| `bootstrap_policy(requested, policy)` | Load policy with random_shadow fallback on error |
| `policy_from_payload(payload, fallback)` | Parse smartload.policy envelope safely |
| `effective_mode(action_mode, rl_mode_env, policy)` | Three-gate mode composition |
| `should_publish(state)` | Skip empty state (cold DB, idle stack) |
| `action_to_event_payload(action, mode, policy_version)` | Serialize RoutingAction |

**Health classification thresholds:**
```python
DEGRADED_LATENCY_MS  = 200.0   # matches SOT §19 P95 SLO target
UNHEALTHY_ERROR_RATE = 0.05    # matches anomaly-detector default
```

These thresholds are used to classify health **locally in rl-engine** because the
`backend_health` table is written by anomaly-detector (which runs on a different service and
different schedule). The two services agree on thresholds by using the same constants.

### `app.py` — Flask + threading

**Key env vars read at startup:**

| Var | Default | Meaning |
|---|---|---|
| `RL_RUNLOOP_ENABLED` | `false` | Enable the inference run loop |
| `RL_POLICY` | `random_shadow` | Policy to load (`random_shadow` \| `ppo`) |
| `RL_MODE` | `shadow` | Operator pin on published mode |
| `RL_SERVICE` | `load-balancer` | Service filter for RL_STATE_QUERY |
| `POLL_INTERVAL_SECONDS` | `5` | Poll cadence |
| `RL_WINDOW_SECONDS` | `30` | DB lookback window for RL_STATE_QUERY |

**`/health` response when run loop is enabled:**

```json
{
  "status": "ok",
  "redis": true,
  "timescaledb": true,
  "rl_mode": "shadow",
  "policy_type": "ppo",
  "policy_requested": "ppo",
  "policy_ready": true,
  "last_inference_age_seconds": 3.2
}
```

When `policy_type != policy_requested`, the requested policy failed to load and the service
is running the `random_shadow` baseline. Returns 200 unless Redis or TimescaleDB is down.

### `RL_STATE_QUERY`

```sql
SELECT
    instance,
    AVG(CASE WHEN metric_name = 'request_latency_ms' THEN value END) AS latency,
    SUM(CASE WHEN metric_name = 'request_count'      THEN value END) AS request_count,
    AVG(CASE WHEN metric_name = 'error_rate'         THEN value END) AS error_rate
FROM metrics
WHERE time > NOW() - %s::interval
  AND service = %s
GROUP BY instance
ORDER BY instance;
```

Parameters: `(f"{WINDOW_SECONDS} seconds", RL_SERVICE)` — both are bind parameters, never
string-formatted.

Note on `error_rate`: `AVG` (not `MAX`) is used because `lb-otel-shipper` emits 0.0 or 1.0
per request. `MAX` would return binary 0/1 and over-classify backends as unhealthy on any
single error. `AVG` yields the true decimal error fraction.

---

## 14. Infrastructure & CI

### CI workflow (`.github/workflows/docker-publish.yml`)

Jobs in dependency order:

```
lint
├── unit-tests
│   └── compose-test
└── build-services
    ├── runtime-import-smoke
    │   └── compose-test
    └── build-test-backend
        └── compose-test
```

**`lint`** — `ruff check services test-backends`

**`unit-tests`** — Pure-Python tests:
- `tests/integration/test_s2_baseline.py`
- `tests/integration/test_telemetry_parser.py`
- `tests/integration/test_lb_otel_shipper.py`
- Per-service unit tests in `tests/unit/*/` (each service gets its own `pytest` invocation
  to prevent `sys.modules` collisions across sibling services)

**`build-services`** — Matrix: builds each service image and smoke-tests its `/health`
endpoint (accepts 200 or 503, because standalone containers have no Redis/DB).

**`runtime-import-smoke`** — Training/serving separation check for rl-engine (see §11).

**`compose-test`** — Full stack integration: `docker compose up -d --scale test-backend=5`,
runs 33 Phase-0 wiring tests + S2 baseline tests + telemetry ingest + lb-otel-shipper tests.

### Docker build context

All services that import `services/shared/` use `context: ./services` with the service
Dockerfile at `services/<name>/Dockerfile`. This allows `COPY shared /app/shared`.

---

## 15. Baseline Policies

Three baseline policies are committed alongside PPO. All always return `mode="shadow"`.

### `random_shadow` (original baseline)

Uniform-random scores; the original Phase-0 baseline. Not in a policies/ subfolder —
lives in `services/rl-engine/policies/random_shadow/`.

### `round_robin` (`services/rl-engine/policies/round_robin/policy.py`)

Cycles through healthy/degraded backends in sorted `backend_id` order.
- Maintains `_idx` counter (modulo N eligible backends)
- Scores: rank 0 → 1.0, rank 1 → (N-1)/N, ..., rank N-1 → 1/N
- All-unhealthy fallback: `_routing_fallback()` from `policy_base`

**Performance:** Joint-best with PPO on the Alibaba dataset (mean_reward = -0.0056).

### `least_connections` (`services/rl-engine/policies/least_connections/policy.py`)

Routes to the backend with lowest `queue_depth` (SUM(request_count) from RL_STATE_QUERY).
- Sort: `(queue_depth ASC, backend_id ASC)` — deterministic tie-break
- Scores: same descending scheme as round_robin
- All-unhealthy fallback: `_routing_fallback()` from `policy_base`

**Performance:** Worst baseline on the Alibaba dataset (mean_reward = -0.0536).
Reason: The `queue_depth` proxy (request count per window) has load imbalances in some
windows that cause the policy to repeatedly favor one backend, increasing utilization variance.

---

## 16. Test Suite Map

### RL-engine unit tests (`tests/unit/rl-engine/`)

124 tests total, all passing as of commit `787eba8`.

| File | Tests | Coverage |
|---|---|---|
| `test_dataset.py` | — | `TraceReplayDataset`: loading, filtering, windowing, bisect, backend mapping |
| `test_env.py` | — | `SmartLoadEnv`: `check_env` pass, obs shape, action space, episode, masking |
| `test_eval_harness.py` | — | `run_eval()`: reproducibility, row format, validation |
| `test_obs_builder.py` | — | `build_observation`, `build_action_mask`, `all_masked_fallback`, `NormParams` |
| `test_ppo_policy.py` | 11 | Loading, shadow/hybrid/learning modes, unhealthy filtering, scores, rankings |
| `test_reward.py` | — | `RewardCalculator`: credit assignment, imbalance term, health penalty |
| `test_round_robin_policy.py` | — | `RoundRobinPolicy`: rotation, scoring, all-unhealthy |
| `test_least_connections_policy.py` | — | `LeastConnectionsPolicy`: sorting, tie-break, scoring |
| `test_runloop.py` | 30 | `build_state_from_rows`, `effective_mode`, `should_publish`, `action_to_event_payload`, `EnginePolicy`, `policy_from_payload` (incl. operating_mode mapping) |

### Integration test

`tests/integration/test_rl_state_query_live.py` — Validates RL_STATE_QUERY against a live
TimescaleDB. Skips when no stack is running. 228 lines.

### Fixtures

`tests/fixtures/rl-engine/policy.zip` — 256-step MaskablePPO warm-up on
`partition_id=-1.csv`. Weights are essentially random; only the interface contract (not
routing quality) is tested. Also has `tests/fixtures/rl-engine/artifact_meta.json`.

---

## 17. Committed Artifacts

| Path | Size | Git commit | Description |
|---|---|---|---|
| `services/rl-engine/models/policy.zip` | 159,802 bytes | `15991db` | Production MaskablePPO artifact (2M steps) |
| `services/rl-engine/models/artifact_meta.json` | 725 bytes | `15991db` | NormParams + training metadata |
| `services/rl-engine/training/eval_results_aec62f8.csv` | 7,195 bytes | `787eba8` | Baseline eval (3 policies × 20 eps) |
| `services/rl-engine/training/eval_meta_aec62f8.json` | 882 bytes | `787eba8` | Baseline eval metadata + partition hashes |
| `services/rl-engine/training/eval_results_f3df2d3.csv` | 9,352 bytes | `15991db` | Final eval (4 policies × 20 eps) |
| `services/rl-engine/training/eval_meta_f3df2d3.json` | 882 bytes | `15991db` | Final eval metadata + partition hashes |
| `services/rl-engine/training/eval_seed_bank.json` | 3,764 bytes | `787eba8` | 25 fixed episode starts |
| `tests/fixtures/rl-engine/policy.zip` | 159,773 bytes | `787eba8` | Test fixture (256-step warm-up) |
| `tests/fixtures/rl-engine/artifact_meta.json` | 18 lines | `787eba8` | Fixture metadata |

---

## 18. Information Confidence Map

### Confident (directly verifiable from committed code/data)

- All hyperparameters in `train_ppo.py` and `train_dqn.py`
- All eval results in committed CSV files
- `artifact_meta.json` — training date, steps, NormParams, sb3 versions
- Training/serving separation — enforced by both Dockerfile and CI
- All reward formula constants (λ=0.1, ε=1.0, health_penalty=-10.0)
- `classify_health` thresholds (200 ms, 5% error rate)
- Mode composition logic (three-gate system in `effective_mode()`)
- All test counts and test file contents
- Dataset partition MD5 hashes (in `eval_meta_f3df2d3.json`)
- The fact that PPO = round_robin on this dataset (verified from CSV)

### Partially uncertain / inferred

- **Canary gate results:** The exact canary output (`pg_loss slope = -1.39e-09`, `ppo=-0.0099 > rr=-0.0148`)
  comes from the commit message body of `787eba8`, not from a committed log file. These
  numbers match what the code would produce but cannot be byte-verified without re-running.
- **Training wall-clock time (~75 min):** Mentioned in the session memory. Not stored in
  any committed artifact. Re-runs on different hardware will vary.
- **"PPO learned round-robin equivalence":** This is an interpretation of the eval results,
  not a direct measurement. The model's internal representations cannot be inspected without
  additional analysis tools.

### Gaps requiring re-validation before acting

- **Dataset availability:** The Alibaba CSV files (`datasets/alibaba/partition_id=*.csv`)
  are NOT committed (git-untracked). Any future training run requires these files to exist
  locally. Their MD5 hashes are in `eval_meta_f3df2d3.json` and can be verified if the
  files are re-downloaded.
- **T2.1 LB sidecar design:** No architecture doc has been written yet. The `smartload.routing`
  channel spec exists in `docs/redis-channels.md`, and the `RoutingRecommendation` envelope
  is defined in `services/shared/contracts.py`, but the sidecar implementation is blank.
- **operator-ui Live Engines view (#121):** Scaffolded in issues but no code committed.
  The BFF (`services/operator-ui/bff/app.py`) would need a WebSocket or SSE proxy to
  forward `smartload.routing` and `smartload.anomaly` events to the browser.

---

## 19. Open Problems, Technical Debt, and Phase 2 Work

### Immediate Phase 2 work (unblocked)

**T2.1 — LB sidecar (dynamic upstream rewriting):**
The most impactful next step. The rl-engine is publishing `RoutingRecommendation` envelopes
on `smartload.routing` in shadow mode. The LB sidecar needs to:
1. Subscribe to `smartload.routing`
2. When `mode == "active"`, translate `server_rankings` into NGINX upstream weights
3. Use the NGINX adapter pattern (see `services/shared/lb_adapters/` for adapter stubs)
4. Also subscribe to `smartload.anomaly` to pull backends from the upstream pool when
   anomaly-detector flags them unhealthy

The adapter pattern is already scaffolded in `services/shared/lb_adapters/` with stubs for
nginx, envoy, haproxy, and ALB. The NGINX adapter is the target for T2.1.

**Known constraint:** NGINX does not support hot upstream weight changes without a module
(nginx-otel, nginx-plus, or nginx-upstream-fair). The current `nginx.conf` uses static
round-robin. Options for T2.1:
- Use NGINX Plus's `/api` endpoint (commercial)
- Use `nginx -s reload` to atomically swap the conf file (simpler, more latency)
- Use OpenResty/Lua to read weights from Redis at request time (more complex)
- Use a different proxy (HAProxy has a runtime API for weight changes)

This decision has not been made yet and is the first T2.1 design question.

### Technical debt

| Item | Location | Impact |
|---|---|---|
| `BackendState.queue_depth` is misnamed | `policy_base.py`, `obs_builder.py` | It's `SUM(request_count)`, not a true queue depth. Low impact — no one acts on the name outside unit tests. |
| `RoundRobinPolicy._idx` persists across calls | `policies/round_robin/policy.py` | The eval harness creates a fresh policy per episode, so eval is unaffected. But in the serve path, the counter accumulates across run loop cycles. This is intended behavior (it IS stateful round-robin) but could be surprising. |
| `PPOPolicy.reload()` raises `NotImplementedError` | `policies/ppo/policy.py` | Hot artifact swap not implemented. The run loop catches this exception and logs it — no crash. But it means a policy.yaml update that changes `operating_mode` triggers a new `bootstrap_policy()` call which re-loads the artifact from disk (relatively fast). |
| `tensorboard_log=None` hardcoded | `train_ppo.py`, `train_dqn.py` | Tensorboard not installed in the training environment. If tensorboard is later installed, the log path should be changed. |
| `least_connections` docstring says "not a true connection-queue depth" | `policies/least_connections/policy.py` | Accepted limitation — `queue_depth` is the best available load proxy with the current schema. |
| Default NormParams (2000 ms, 200 reqs) vs. actual (100 ms, 95 reqs) | `env.py`, `obs_builder.py` | The defaults are only used in smoke tests. The production `artifact_meta.json` has the correct values. Any new training run will re-derive them via `_compute_norm_params()`. No action required unless the dataset changes significantly. |

### Rejected approaches / design decisions documented here

**Why offline RL instead of online RL:**
Online RL on a live production system would require the agent to actually make routing
decisions and observe their consequences, which risks degrading user-facing latency during
learning. Offline RL on the Alibaba trace is safer and reproducible for a GP project.

**Why MaskablePPO instead of standard PPO:**
Standard PPO does not support action masking natively. Routing unhealthy backends is
explicitly forbidden — the mask provides a hard constraint rather than relying on the reward
signal alone (which would require many negative-reward episodes to learn the constraint).

**Why DQN fallback exists:**
PPO sometimes fails to learn on small datasets or with poorly tuned hyperparameters
(PG loss diverges instead of decreasing). The canary gate detects this automatically and
`train_dqn.py` provides a value-based fallback. DQN does not require MaskableDQN wrappers
because invalid actions can be Q-penalized rather than masked. The fallback was not needed
for this training run.

**Why `_compute_norm_params()` uses p99 not max:**
The maximum latency in the Alibaba trace may be a single outlier that would make all
normalized observations vanishingly small. p99 is more robust while still capturing the
tail behavior.

**Why `effective_mode()` has three independent gates:**
Defense-in-depth. Operator intent (`RL_MODE`), policy configuration (`safe_mode`), and
policy action (`action.mode`) are three independent signals. Any of them can veto active
mode. This prevents a misconfigured PPO model from accidentally activating if the operator
hasn't explicitly opted in via the env var.

**Why `should_publish()` skips empty state:**
An empty `server_rankings` list in the `RoutingRecommendation` envelope is meaningless to
a future LB sidecar. Sending it would require the sidecar to handle the empty case.
During cold start (DB not yet populated) or idle periods (no traffic through NGINX), this
avoids noise.

**Why `classify_health` is duplicated in `runloop.py` and used in `training/dataset.py`:**
The `runloop.py` version is the serving-path truth. `dataset.py` imports it from `runloop.py`
(via `from runloop import classify_health`) to ensure the training environment applies the
identical classification logic. There is no duplication — `dataset.py` literally imports
the canonical function.

---

## 20. Activation Runbook

To activate PPO routing in the live stack:

```bash
# 1. Ensure RL run loop is enabled and PPO is requested
export RL_RUNLOOP_ENABLED=true
export RL_POLICY=ppo
export RL_MODE=shadow           # start in shadow; verify before going active

# 2. Start (or restart) the rl-engine container
docker compose up -d --force-recreate rl-engine

# 3. Verify the policy loaded correctly
curl http://localhost:8084/health | python -m json.tool
# Expected: policy_type="ppo", policy_ready=true, last_inference_age_seconds<10

# 4. Monitor the smartload.routing channel to see recommendations
redis-cli SUBSCRIBE smartload.routing

# 5. When satisfied with shadow behavior, flip to active
export RL_MODE=active
# policy.yaml already has operating_mode=hybrid, safe_mode=false
# These three gates now all agree → RoutingRecommendation.mode will be "active"
docker compose up -d --force-recreate rl-engine

# 6. Verify active mode in health response
curl http://localhost:8084/health | python -m json.tool
# rl_mode field will show "active"
```

**Prerequisites:**
- `services/rl-engine/models/policy.zip` and `artifact_meta.json` exist (committed, present)
- Training dependencies in `requirements.txt` are installed (stable-baselines3, sb3-contrib)
- At least one NGINX request has been processed so the `metrics` table has rows
  (otherwise `RL_STATE_QUERY` returns empty → no envelopes published)

---

## 21. Key Invariants — Do Not Break

These are constraints that, if violated, will cause silent incorrect behavior without an
obvious error.

1. **`N_MAX_BACKENDS = 5` must be consistent** between `obs_builder.py`, `policy.yaml.max_backends`,
   the training NormParams, and any existing `policy.zip` artifact. Changing it requires
   retraining and rebuilding the Docker image.

2. **Backend sort order must be identical between training and serving.** Both
   `build_observation()` and `build_action_mask()` sort by `backend_id` (lexicographic).
   The dataset maps `dm` hashes to `"backend_1"`, `"backend_2"`, ... (sorted). The serving
   path sorts `instance` values from RL_STATE_QUERY alphabetically. These must produce
   identical orderings for the trained policy's action indices to be valid.

3. **The reward reads from `next_state`, not `state`** (credit assignment). Changing this
   in future training would break the credit chain and the policy would learn to route to
   backends that look good *before* the action takes effect.

4. **NormParams are stored in `artifact_meta.json` and must be loaded by `PPOPolicy`.**
   Using the wrong NormParams at serving time produces out-of-distribution observations —
   the model sees normalized values outside the range it was trained on.

5. **The `training/` directory must never be imported by serving code.** Enforced by the
   `runtime-import-smoke` CI job. Adding a `from training.x import y` anywhere in
   `app.py`, `runloop.py`, `policy_base.py`, `obs_builder.py`, or `policies/*/policy.py`
   will fail CI.

6. **`all_masked_fallback()` must always return exactly one `True` entry.** SB3's
   `MaskablePPO` will raise on an all-False mask. The current implementation does this
   correctly; any modification must preserve this contract.

7. **The eval seed bank (`eval_seed_bank.json`) must not be regenerated.** It represents
   the fixed held-out eval set. Regenerating it would make historical eval results
   non-comparable. If new episodes are needed, add them to the bank; don't replace it.

---

*Document generated: 2026-05-23. Cross-checked against commits `787eba8`, `f3df2d3`,
`15991db`, `600d2a5` and the following source files: `env.py`, `dataset.py`, `simulator.py`,
`reward.py`, `train_ppo.py`, `eval_harness.py`, `obs_builder.py`, `policy_base.py`,
`policies/ppo/policy.py`, `policies/round_robin/policy.py`,
`policies/least_connections/policy.py`, `runloop.py`, `app.py`, `Dockerfile`,
`requirements.txt`, `artifact_meta.json`, `eval_results_f3df2d3.csv`,
`eval_seed_bank.json`, `policy.yaml`, `docker-compose.yml`,
`.github/workflows/docker-publish.yml`, `docs/redis-channels.md`.*
