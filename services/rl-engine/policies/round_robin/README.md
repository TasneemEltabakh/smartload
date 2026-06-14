# round_robin policy

Stateful cyclic scheduler over eligible backends. Always emits `mode=shadow` so the rankings never affect live routing on their own.

## Algorithm
1. Filter to eligible backends via `is_eligible()` (excludes `unhealthy` and `unknown`; degraded backends still serve).
2. Sort by `backend_id` for a stable slot order.
3. Pick the head: the lowest `backend_id` strictly greater than the last-served id (wraps to the first when past the highest, or on the first call).
4. Assign descending scores so the head leads: rank 0 → 1.0, rank 1 → (N-1)/N, …, rank N-1 → 1/N.

## Why a backend_id pointer, not a modular index
The eligible set changes as backends go degraded/unknown/unhealthy. A modular index (`idx % len(eligible)`) reshuffles the whole rotation whenever the set size changes. A `backend_id` pointer is stable: removing the just-served backend still advances to the next id, and adding one does not reset the cycle.

## Why this ships
A deterministic classical baseline for the router and for benchmark comparison against the learned policy — the same scheduling NGINX's default `round-robin` expresses, but computed in the decision plane so it can be A/B'd against `least_connections` and `ppo`. All-unhealthy / no-eligible input falls back to `policy_base._routing_fallback()`.

## Tests
- `test_policy.py` — shadow mode, one ranking per eligible backend, unhealthy/unknown exclusion, unit-interval scores, rotation advance + wrap, pointer stability when the eligible set shrinks, all-unhealthy fallback.
