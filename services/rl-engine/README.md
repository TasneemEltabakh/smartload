# rl-engine

Routing decision engine. Publishes `RoutingRecommendation` to `smartload.routing`. Runs in shadow mode by default; can flip to active when policy `operating_mode=hybrid` and a trained policy is loaded.

The trained `ppo` policy is a **contextual bandit** (state → chosen backend) optimised with MaskablePPO on logged Alibaba traces — not an MDP-trained RL agent. The offline simulator replays trace windows independently of the agent's action, so the model learns to predict which backend will have low latency in the next window, not the consequence of its routing on the system. The closed-loop "consequence" axis lives in the deterministic safety machinery (NGINX `max_fails`, anomaly-detector exclusions, autoscaler reactivity). See `services/rl-engine/training/simulator.py` for the framing rationale.

## Role
- Polls TimescaleDB every `POLL_INTERVAL_SECONDS` for current per-backend state
- Runs the configured policy (random / round-robin / least-connections baselines + ppo)
- Publishes `RoutingRecommendation` with `mode=shadow` or `mode=active`

## Policies
Pluggable — one folder per policy. See `policies/`.
- `random_shadow/` — uniform-random backend ranking, mode always shadow (sanity baseline)
- `round_robin/` — stable backend_id-pointer rotation across eligible backends
- `least_connections/` — ranks by ascending `queue_depth` (= window request count) among eligibles
- `ppo/` — contextual bandit trained with MaskablePPO; argmax-dominant weighting at serve time

Selection: `RL_POLICY` env var.

## Backend eligibility (health gating)

Health states (`policy_base.HEALTH_*`):
- `healthy` / `degraded` → **eligible** to receive routed traffic
- `unhealthy` → excluded (anomaly detector verdict, or local error-rate above threshold)
- `unknown` → excluded (no telemetry signal in the query window; silence is not "healthy")

All policy plugins use `policy_base.is_eligible()` so the eligibility predicate stays in one place — `policies/*/policy.py`, `obs_builder.build_action_mask`, and the LB sidecar all agree on what "eligible" means.

The anomaly-detector's verdicts on `smartload.anomaly` take precedence over local classification per SOT §9 health ownership; the local fallback applies only to backends not yet seen on the anomaly channel. Anomaly verdicts time out after `ANOMALY_HEALTH_TTL_MULTIPLIER × RL_WINDOW_SECONDS` (default 3 min) so the dict doesn't grow without bound under backend churn.

## Redis channels
- Subscribes: `smartload.policy`
- Publishes: `smartload.routing`

## Env vars
- `TIMESCALEDB_URL`, `REDIS_URL`
- `RL_RUNLOOP_ENABLED` (default `true` since v1.0.7g; was `false` before) — set to `false` to revert to the Phase-0 stub (no run loop, `/health` only). The `RL_MODE=shadow` default below is the routing-safety pin: even with the run loop on, the LB sidecar ignores any envelope whose `mode != "active"`. See SOT §8.7 + issue #138.
- `RL_POLICY` (default `random_shadow`) — `random_shadow` | `ppo`. If the requested policy fails to load (e.g. missing `policy.zip`), the service falls back to `random_shadow` and reports `policy_ready=false` on `/health`.
- `RL_MODE` (default `shadow`) — `shadow` | `active`. **Operator pin on the published `mode` field.** Even if the loaded policy would emit `mode=active`, the run loop forces `shadow` unless `RL_MODE=active` AND the policy itself agrees AND `safe_mode=false` in the operating policy.
- `POLL_INTERVAL_SECONDS` (default 5)
- `RL_WINDOW_SECONDS` (default 30) — DB lookback window passed to `RL_STATE_QUERY`.

## /health response

When the run loop is enabled, `/health` adds four engine fields plus the existing `rl_mode`:

```json
{
  "status": "ok",
  "redis": true,
  "timescaledb": true,
  "rl_mode": "shadow",
  "policy_type": "random_shadow",
  "policy_requested": "ppo",
  "policy_ready": false,
  "last_inference_age_seconds": 4.1
}
```

`policy_ready=false` with `policy_type != policy_requested` means the requested policy couldn't load and the service is running the random-shadow baseline. Returns 200 unless Redis or TimescaleDB is unreachable (then 503).

## Mode-composition rules

The published `RoutingRecommendation.mode` field is composed from three inputs in this order:

1. **`safe_mode=true` in operating policy** → always `"shadow"` (operator hard-pause)
2. **`RL_MODE != "active"` env var** → `"shadow"` (operator hasn't opted in)
3. **`RL_MODE=active` AND policy returned `mode="active"`** → `"active"` (LB sidecar applies the weights)
4. Any other combination → `"shadow"` (default safety)

Both env and action-mode comparisons are case-insensitive so a stray `Active` in either input is accepted.

This keeps decisions observable even when not enacted, supporting explainability + the operator UI Live Engines view (#121).

## Safety boundary — who owns what

The same intent (don't actually move traffic) appears in three places. They are intentionally redundant but have a single source of truth:

| Component | Role | Authority |
|---|---|---|
| `runloop.effective_mode` | Telemetry hygiene | Marks the published envelope `mode=shadow` so downstream consumers and the Live Engines UI never claim "active" when the operator pin says otherwise. |
| `lb-sidecar.handle_routing` | **Safety boundary** | The only place that actually rewrites NGINX upstream weights. Refuses to apply anything except `mode=="active"`. Even if rl-engine misbehaved, the sidecar would not propagate it. |
| `lb-sidecar.handle_policy` | Reset on hard-pause | When a policy update arrives with `safe_mode=true`, resets the upstream to equal weights. |

**Rule of thumb**: when the question is "could this make NGINX move traffic in a way the operator didn't approve?", the answer lives in the LB sidecar, not here. The rl-engine gate prevents telemetry confusion, not unsafe routing.

## Argmax-dominant weighting (PPO)

The trained PPO policy was optimised for `Discrete(N_MAX_BACKENDS)` — picks one backend per step. At serve time the policy emits a full ranking: the chosen (argmax) backend gets `_DOMINANT_WEIGHT` (default 0.7) and the remaining mass is split evenly among other eligible backends. This matches the training objective while still leaving a floor for NGINX health probing of the rest. See `policies/ppo/policy.py` for the implementation.

## Status

- Phase 0 stub: `/health` only — **default**
- Phase 1 run loop (this folder): wired behind `RL_RUNLOOP_ENABLED`. All four baseline policies + the trained PPO bandit ship today.

## Integrating a trained policy

To swap in a trained policy artifact:
1. Drop the artifact at `services/rl-engine/models/policy.zip` (the canonical name from SOT §8.7).
2. Implement `policies/<name>/policy.py` exporting `class <Name>Policy(RoutingPolicy)` that loads the artifact in `__init__` and implements `act(state) -> RoutingAction`. Override `reload(**kwargs)` if the policy has mutable runtime params (operating_mode, exploration rate) — the run loop calls it on every `smartload.policy` publish so you can update without a torch reload.
3. Register the policy name in `policy_base.select_policy()`.
4. Set `RL_POLICY=<name>` in the deployment env.
5. To take routing recommendations live, also set `RL_MODE=active` AND ensure the policy itself returns `mode="active"`.

The run loop, Redis publishing, policy subscription, state classification, mode composition, and fallback-to-baseline are all owned by `app.py` + `runloop.py` — the model author writes only `act(state) -> RoutingAction` and optionally `reload(**kwargs)`.

## See also
- Feature manifest: `docs/features/rl-routing.md` (pending — see SOT §25.9 slice catalog)
- Issues: #138 (engine-wrapper foundation), #27 (PPO training), #29 (shadow scaffold), #28 (active mode)
