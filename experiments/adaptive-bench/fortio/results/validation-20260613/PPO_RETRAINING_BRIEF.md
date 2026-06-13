# PPO routing — findings + retraining recommendations

**Context for Rghda.** We replaced the test backends with a closed-loop M/G/c
queue model (latency now rises with load) and validated end-to-end on the full
stack with `RL_POLICY=ppo, RL_MODE=active`. The realistic backend exposes a
routing behaviour that the old constant-delay backend hid. This brief is the
evidence + what the retrain needs to change so PPO can actually balance load.

## What PPO does today (measured)

- **It concentrates.** PPO's live output is a stable weighting of **0.70 on one
  arbitrary backend, ~0.075 on each of the other four** (the argmax-dominant
  serving rule), and it does **not** change across offered load from 0 to 900
  QPS on a homogeneous pool.
- **On a realistic backend that is ~2× worse than round-robin.** At a stable
  60 QPS (pool not overloaded), measured at the load balancer:

  | Routing | request distribution | p50 | p99 |
  |---|---|---:|---:|
  | PPO (0.70 concentrated) | 69% to one backend | **158 ms** | **665 ms** |
  | plain round-robin | 20% each | **86 ms** | **241 ms** |

  Same workload, same backends; the only difference is the weight vector. Under
  load the concentrated backend queues while four sit idle, so the median
  request waits behind a queue. At higher load it is worse: at 500 QPS PPO
  served 21% of requests vs 100% for round-robin (it collapses effective pool
  capacity).
- **It does react to a degraded backend.** When we slowed PPO's favoured backend
  (+250 ms), PPO moved its weight off it (0.70 → ~0.01) within ~20 s via the
  routing path. So PPO is latency-reactive — but its reaction only **relocates
  the 70% lump to a different backend**; it never learns to spread the load.

## Root cause (why it concentrates)

The policy was trained in an **open-loop simulator**: the agent replays trace
windows and the backend latencies it observes are **independent of its own
routing decisions** — there is no queue that the agent's concentration can build
up. So in training, sending 70% to one backend was **free** (no latency
penalty), and the optimal learned behaviour is exactly that: pick the
single best-looking backend and exploit it. On the old constant-delay backend
this was also free, so it looked harmless / round-robin-equivalent. A realistic
queueing backend is the first environment where concentration has a cost — and
that cost is what the table above shows.

## What the retrain needs (in priority order)

1. **Close the training loop — highest impact.** The simulator must model
   backend queueing so the agent's own actions perturb the latency it observes.
   Reuse the closed-loop model we just shipped: `test-backends/lib/pool.js`
   (`WORKERS` service slots + a bounded `QUEUE_MAX` FIFO + 503 shed) and
   `test-backends/lib/service_time.js` (lognormal service time, mean 20 ms,
   cv 1). Port those dynamics into the Gym env (`SmartLoadEnv`) so observed
   per-backend latency = queue-wait + service-time given the agent's weights.
   Without this, no reward change will teach it to spread — concentration stays
   costless in training.

2. **Reward shaping for balance, not just latency-of-chosen.** Reward the agent
   on a pool-level objective that punishes imbalance: e.g. negative pool **p95**,
   or negative **max** per-backend queue depth / utilisation, rather than the
   mean or the chosen backend's latency. Concentration must lower the reward.

3. **Action representation.** The argmax-dominant 0.70 serving rule concentrates
   by construction. Have the policy emit a full **weight distribution** (softmax
   over backends) and, on a homogeneous pool, reward it for producing a near-
   uniform spread. Keep the sharp argmax only for genuinely heterogeneous state.

4. **State features.** Include per-backend **in-flight / queue depth** and recent
   observed latency in the observation (now exposed at `/_admin/stats`), so load
   imbalance is actually visible to the policy. Train across homogeneous +
   heterogeneous + single-backend-anomaly scenarios (a curriculum).

5. **Promotion gate before leaving shadow.** Two hard checks, both evaluated
   against the closed-loop backend (use the Fortio probe at a fixed QPS):
   - **Homogeneous:** PPO p50/p99 ≤ round-robin (today it loses ~2×).
   - **One degraded backend:** PPO ≥ least-connections at steering away.
   Until it passes both, keep `RL_MODE=shadow` and default routing to a classical
   policy (round-robin / least-connections).

## How to reproduce / evaluate

The validation harness is in this folder:
`experiments/adaptive-bench/fortio/` (open-loop probe) +
`results/validation-20260613/` (`run_arm.py` drives a fixed-QPS arm and records
per-backend distribution + LB latency; `SUMMARY.md` has the full method/data).
Bring the stack up, set the policy via `RL_POLICY` + `RL_MODE=active`, and run
`run_arm.py <label> <qps> <duration>`. Compare against even round-robin
(upstream weights 20/20/20/20/20) and least-connections.

**Bottom line:** PPO isn't being held back by data alone — it's the open-loop
training environment. Give it a queueing simulator + a balance-aware reward + a
distributional action, and re-gate it against round-robin on the realistic
backend before promotion.
