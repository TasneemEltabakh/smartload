"""
services/rl-engine/policies/least_connections/policy.py
────────────────────────────────────────────────────────
LeastConnectionsPolicy — routes to the backend with the lowest current load.

Algorithm
─────────
1. Filter state to backends where health != "unhealthy".
2. Sort by (queue_depth ASC, backend_id ASC).
   Tie-break rule: lower backend_id wins.  This is explicit and deterministic;
   callers can rely on the same outcome given identical state.
3. Assign descending scores: rank 0 → 1.0, rank 1 → (N-1)/N, ..., rank N-1 → 1/N.
4. Always returns mode="shadow".

NOTE: BackendState.queue_depth is SUM(request_count) from RL_STATE_QUERY — not a
true connection-queue depth.  It is the best available load proxy in the current
schema; the algorithm name reflects intent rather than the exact metric.

All-unhealthy fallback: policy_base._routing_fallback() — same canonical path as
RoundRobinPolicy.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from policy_base import (      # noqa: E402
    BackendState,
    Ranking,
    RoutingAction,
    RoutingPolicy,
    _routing_fallback,
    is_eligible,
)


class LeastConnectionsPolicy(RoutingPolicy):
    """Ranks backends by ascending request load; lowest-loaded is preferred.

    Unknown-health backends are excluded — picking the least-loaded among
    backends with no signal would route to whichever happens to be silent,
    which is the opposite of intent.
    """

    def act(self, state: list[BackendState]) -> RoutingAction:
        eligible = [b for b in state if is_eligible(b.health)]
        if not eligible:
            return _routing_fallback(state)

        # Primary: queue_depth ASC; secondary: backend_id ASC (deterministic tie-break)
        ranked = sorted(eligible, key=lambda b: (b.queue_depth, b.backend_id))
        n = len(ranked)

        rankings = [
            Ranking(backend_id=b.backend_id, score=(n - i) / n)
            for i, b in enumerate(ranked)
        ]
        return RoutingAction(mode="shadow", rankings=rankings)
