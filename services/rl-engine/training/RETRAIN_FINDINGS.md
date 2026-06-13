# Closed-loop PPO retrain — findings & handoff (2026-06-13)

Branch `feat/ppo-causal-retrain`. Companion to the live validation in
`experiments/adaptive-bench/fortio/results/validation-20260613/` (which showed
the *shipped* PPO concentrates 70% on one backend → ~2× worse than round-robin
once backends actually queue).

## What was changed (the architecture fix)

The shipped policy was trained as an **open-loop bandit** (`simulator.py`:
`step(action)` ignores the action; reward = the *recorded* latency of the chosen
backend). Concentration was costless in training, so it learned to concentrate.

This retrain closes the loop, additively (old files untouched):

| New module | Role |
|---|---|
| `closed_loop_sim.py` | Causal M/G/c queue sim. Action = weight vector; each backend's latency is computed from the load it receives. Heterogeneous backends + synthetic demand + mid-episode degradation (curriculum: ~25% homogeneous / 35% heterogeneous / 40% degrading). No Alibaba dataset needed. |
| `env_v2.py` + `reward_v2.py` | Box(weights) action via masked softmax; reward = −served_latency − w·tail − w·shed, all causal. |
| `train_ppo_v2.py` | SB3 PPO (continuous). Writes `policy_v2.zip` + `artifact_meta_v2.json` (`policy_kind=continuous_weights`). |
| `eval_gates_v2.py` | Per-scenario promotion gates (homogeneous / heterogeneous / degrading). |
| `policies/ppo/policy.py` | Serving: new `continuous_weights` branch emits the full softmax weight vector (not argmax-dominant 0.7). Old discrete path kept for back-compat. |

## The key training finding (γ=0)

First run (γ=0.95, 400k steps): PPO learned a **state-blind near-uniform**
policy (on a 300 ms backend it still routed ~0.20 there) and was *worse* than
round-robin (−3.33 vs −2.54). Root cause: the queueing consequence lands in the
**same window's** reward, so this is a contextual bandit — with γ=0.95 the value
function chased the unobservable random demand and drowned the routing signal in
advantage noise.

Fix: **γ=0** + reward normalisation + curriculum tilt. Second run (600k steps)
learned genuine state-adaptive routing.

## Results (held-out, candidate = `models/candidate_v2/`)

Mean reward over 60 mixed episodes:

| policy | reward | served latency |
|---|---:|---:|
| **PPO-v2** | **−2.08** | **144 ms** |
| round-robin | −2.87 | 183 ms |
| least-connections | −2.50 | 247 ms |

Per-scenario served latency (gate run):

| policy | homogeneous | heterogeneous | degrading |
|---|---:|---:|---:|
| **PPO-v2** | 100.0 | **120.3** | **198.7** |
| round-robin | **41.1** | 206.1 | 275.1 |
| least-connections | **41.1** | 183.4 | 309.1 |
| concentrate (old PPO) | 313.8 | 420.3 | 502.0 |

Adaptive behaviour (serving, deterministic weights):
- homogeneous → ~even (0.185–0.215)
- backend_1 @300 ms → 0.043 on the bad one; backend_3 @300 ms → 0.02.

## Promotion gates: **MIXED**

- **Gate B (degrading): PASS** — PPO 199 ms ≤ least-conn 309 ms.
- **Gate A (homogeneous): FAIL** — PPO 100 ms vs round-robin 41 ms.

PPO wins the mixed / heterogeneous / degrading workloads decisively, but
**regresses on healthy homogeneous load**: the optimum there is *exact* uniform,
and a learned continuous policy emits ~±8% imbalance, which near saturation is
costly. So by the strict two-gate bar **it has NOT earned promotion** — but it is
a large, real improvement over the shipped artifact (which was worse than
round-robin everywhere), and a strong candidate when heterogeneity/anomalies are
present.

## Two paths to close Gate A (Rghda's call)

1. **Hybrid serving guard (cheap, deployable now):** when the observed
   per-backend latency spread is below ε (pool looks homogeneous), snap to
   uniform; otherwise use the learned weights. Captures the adaptive wins with no
   homogeneous regression. (A `safe_mode`-style guard, not reward-hacking.)
2. **Uniform-prior action / more training:** parameterise the action as a
   *deviation* from uniform with a small L2 penalty so the natural default is
   exact-even, and/or train longer with entropy decay so the homogeneous output
   tightens.

## Reproduce

```
pip install -r requirements-training.txt   # torch CPU ok
python training/test_closed_loop_sim.py     # sim premise smoke (no GPU)
python training/train_ppo_v2.py --steps 600000 --out-dir training/_staging
python training/eval_gates_v2.py --model training/_staging/policy_v2 \
       --meta training/_staging/artifact_meta_v2.json
```

Promotion (`RL_MODE=active` + swapping `models/candidate_v2/` → `models/policy.*`)
is intentionally left to the operator/Rghda once Gate A is closed.
