# least_connections policy

Routes to the backend with the lowest current load. Always emits `mode=shadow` so the rankings never affect live routing on their own.

## Algorithm
1. Filter to eligible backends via `is_eligible()` (excludes `unhealthy` and `unknown`).
2. Sort by `(queue_depth ASC, backend_id ASC)` — lower load wins, lower `backend_id` breaks ties deterministically.
3. Assign descending scores: rank 0 → 1.0, rank 1 → (N-1)/N, …, rank N-1 → 1/N.

## Load proxy caveat
`BackendState.queue_depth` is `SUM(request_count)` from `RL_STATE_QUERY`, not a true connection-queue depth. It is the best available load proxy in the current schema; the policy name reflects intent rather than the exact metric.

## Why this ships
A classical load-aware baseline for the router and for benchmark comparison against `round_robin` and the learned `ppo` policy — the decision-plane equivalent of NGINX's `least_conn` directive, computed centrally so it can be A/B'd. All-unhealthy / no-eligible input falls back to `policy_base._routing_fallback()`.

## Tests
- `test_policy.py` — shadow mode, lowest-load preference, strictly descending scores with load, deterministic `backend_id` tie-break, unhealthy/unknown exclusion, unit-interval scores, all-unhealthy fallback.
