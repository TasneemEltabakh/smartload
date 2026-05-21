# rl-engine

Reinforcement-learning routing engine. Publishes `RoutingRecommendation` to `smartload.routing`. Runs in shadow mode by default; can flip to active when policy `operating_mode=hybrid` and a trained policy is loaded.

## Role
- Polls TimescaleDB every `POLL_INTERVAL_SECONDS` for current per-backend state
- Runs the configured policy (`random_shadow` baseline; `ppo` swap planned)
- Publishes `RoutingRecommendation` with `mode=shadow` or `mode=active`

## Policies
Pluggable — one folder per policy. See `policies/`.
- `random_shadow/` — baseline; uniform-random backend ranking, mode always shadow
- `ppo/` — stub today; trained `policy.zip` drop-in planned

Selection: `RL_POLICY` env var.

## Redis channels
- Subscribes: `smartload.policy`
- Publishes: `smartload.routing`

## Env vars
- `TIMESCALEDB_URL`, `REDIS_URL`
- `RL_RUNLOOP_ENABLED` (default `false`) — flip to `true` to start the inference run loop. Off by default so the Phase-0 stub stays the safe default until operators opt in. See SOT §8.7 + issue #138.
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

This keeps RL output observable even when not enacted, supporting explainability + the operator UI Live Engines view (#121).

## Status

- Phase 0 stub: `/health` only — **default**
- Phase 1 run loop (this folder): wired behind `RL_RUNLOOP_ENABLED`. Random-shadow baseline ships; PPO plugin scaffolded, awaits trained `policy.zip` (#27, ~7-day training run).

## Integrating a trained policy

To swap in a trained PPO policy:
1. Drop the artifact at `services/rl-engine/models/policy.zip` (the canonical name from SOT §8.7).
2. Implement `policies/ppo/policy.py` exporting `class PPOPolicy(RoutingPolicy)` that loads the artifact in `__init__` and implements `act(state) -> RoutingAction`.
3. Register the policy name in `policy_base.select_policy()` (already done).
4. Set `RL_POLICY=ppo` in the deployment env.
5. To take routing recommendations live, also set `RL_MODE=active` AND ensure the PPO policy itself returns `mode="active"`.

The run loop, Redis publishing, policy subscription, state classification, mode composition, and fallback-to-baseline are all owned by `app.py` + `runloop.py` — the model author writes only `act(state) -> RoutingAction`.

## See also
- Feature manifest: `docs/features/rl-routing.md` (pending — see SOT §25.9 slice catalog)
- Issues: #138 (engine-wrapper foundation), #27 (PPO training), #29 (shadow scaffold), #28 (active mode)
