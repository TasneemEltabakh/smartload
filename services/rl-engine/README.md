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
- `RL_POLICY` (default `random_shadow`)
- `POLL_INTERVAL_SECONDS` (default 5)

## Status
Service skeleton shipped (Phase 0). Policy implementations land in `policies/`.

## See also
- Feature manifest: `docs/features/routing-decisions.md` (pending)
- Issues: #29 (shadow scaffold), #27 (PPO training), #28 (active mode)
