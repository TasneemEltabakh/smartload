# ppo policy

PPO-trained routing policy from Stable-Baselines3. Replaces the random-shadow baseline when `RL_POLICY=ppo` and `services/rl-engine/models/policy.zip` is present.

## Status

Scaffolded only. Implementation lands when:
- the SmartLoadEnv Gym environment is built (issue #27)
- the PPO checkpoint is trained (issue #27)

## Planned files

- `policy.py` — `PPOPolicy(RoutingPolicy)` that loads policy.zip on init, falls back to random_shadow if missing
- `test_policy.py` — fixture-based tests with a small saved policy

## Mode transitions

When the policy is loaded and `operating_mode=hybrid`, the policy reports `mode=active` instead of `mode=shadow`. The LB sidecar starts honouring the rankings.
