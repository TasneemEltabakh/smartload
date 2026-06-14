# monotone policy

Serving plugin for the latency-monotone capacity-aware router (`candidate_mono`). Loaded by `select_policy("monotone")`. Emits `mode=shadow` by default; `operating_mode` of `hybrid` or `learning` promotes it to `mode=active`.

## Algorithm
1. Filter to eligible backends via `is_eligible()` (excludes `unhealthy` and `unknown`).
2. Maintain a per-backend capacity estimate as the running minimum observed latency (floored at `cap_floor_ms`). The estimate depends on PAST latencies only.
3. Score each eligible backend as `cap / (lat / base) ** degr_pow`, where `cap = 1 / base` and `base` is the capacity estimate. A backend whose current latency exceeds `cut * min_latency` is suppressed (score scaled by 1e-3).
4. Damp toward the new target weights: full step when total load is below `idle_load`, otherwise blend with rate `alpha`. The damped vector is renormalised to sum to 1.

## Config
Read from `params.json` (`monotone_config` block, same shape `training/train_monotone.py` writes), defaulting to `models/candidate_mono/`. Keys: `degr_pow`, `alpha`, `cut`, `cap_floor_ms`, `idle_load`. A missing or unreadable artifact falls back to built-in defaults.

## Monotone by construction
Holding history fixed, a backend's score is strictly decreasing in its current latency, and the capacity estimate uses only past latencies. So routing weight never increases with a backend's latency — the property the benchmark's latency-monotonicity probe verifies.

## Serving / training separation
Imports only `obs_builder` and `policy_base`; the router math is inlined here, kept equivalent to `training/monotone_router.MonotoneRouter` so train and serve agree. It never imports from `training/`.

## Tests
- `test_policy.py` — default shadow mode, hybrid/learning → active, lower-latency preference, weight monotonicity in latency, unhealthy/unknown exclusion, all-unhealthy and empty-state fallback, normalised unit-interval weights.
