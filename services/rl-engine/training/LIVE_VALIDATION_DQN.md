# DQN-templates — live Fortio validation (2026-06-13)

Branch `feat/ppo-causal-retrain`. Validated the `models/candidate_dqn` artifact
**end-to-end on the running stack** (real rl-engine → lb-sidecar → NGINX signal
flow), not just in the simulator.

## How it was run
- Added serving support for `policy_kind="discrete_templates"`: new serving-safe
  `routing_templates.py` (shared with the training env) + a DQN branch in
  `policies/ppo/policy.py` that maps the chosen template id to NGINX weights.
- Deployed by `docker cp`-ing the serving code + the DQN artifact into the
  running `rl-engine` and restarting it (reversible — a `--force-recreate`
  restores the baked production image). Pool scaled to 5, autoscaler frozen.
- Load via the open-loop Fortio probe at the LB.

## Results

**1. Serving integration — PASS.** rl-engine loaded the artifact
(`kind=discrete_templates type=dqn`, `ready=True rl_mode=active`) and published
routing recommendations the sidecar applied.

**2. Homogeneous pool under load — PASS (routes ~uniform).** At 80 QPS the live
NGINX weights converged to ~even within ~16 s (`20/20/20/20/20`), and Fortio saw
**100% success (3600/3600), 25 ms avg**. No concentration — the original 70/8
PPO pathology is gone.

**3. Degraded-backend reroute — PASS (the headline).** With 80 QPS flowing, we
injected +250 ms on backend-3. DQN steered traffic off it via the routing path:
weight **22 → 8 → 4 → 3 → 1** over ~32 s, the other four absorbing the slack
(~25 each). This is the adaptive value, proven live.

**4. Quantitative A/B (degraded backend) — directional only.** DQN p99 ≈ 2111 ms
vs round-robin p99 ≈ 3901 ms (RR keeps routing 1/5 to the 270 ms backend). DQN
wins, but both numbers are noisy — see caveats.

## Caveats (honest)

- **OOD when idle / lightly-loaded.** The policy was trained only on *loaded*
  scenarios, so on a near-idle pool (observation ≈ all-zeros) it goes
  out-of-distribution and **concentrates** (it picked `100/1/1/1/1` at idle, and
  in the A/B — where the degrade happened during an idle wait — it concentrated
  on backend-5 instead of spreading). It re-spreads correctly within ~10–16 s
  once sustained in-distribution load arrives. This is a real production concern
  (traffic is bursty); it argues for either training with idle/low-load
  scenarios, or a serving guard that defaults to uniform when observed load is
  below a floor.
- **Single-host CPU contention** still confounds absolute latency (see the
  earlier validation), so the A/B p99 numbers are directional, not definitive.
- Single-seed model, sim-trained. Multi-seed + a quieter measurement host are
  needed before promotion.

## Verdict
The DQN-templates candidate is **functionally validated live**: it serves through
the real plane, routes ~uniformly on a healthy pool, and **reroutes away from a
degraded backend** — the behaviour the whole retrain was for. The blocking issue
for promotion is now the **idle/OOD concentration**, not the core routing logic.
Recommended before `RL_MODE=active` in production: add low-load idle scenarios to
the curriculum (or a uniform-default serving floor), then re-validate.

Stack restored to the production artifact (`discrete_argmax`) after the test;
nothing left swapped in.
