# random_shadow policy

Uniform-random backend ranking. Always emits `mode=shadow` so the rankings never affect live routing.

## Why this ships

It validates the full RL pipeline (state → policy → action → envelope → Redis → subscriber) without needing a trained model. When the PPO model lands and is loaded, the active engine reports `mode=active` and the LB sidecar starts honouring weights.

## Tests

- `test_policy.py` — confirms uniform-random output and consistent envelope shape.
