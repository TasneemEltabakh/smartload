"""
services/rl-engine/policies/round_robin/policy.py
──────────────────────────────────────────────────
RoundRobinPolicy — stateful cyclic scheduler over eligible backends.

Algorithm
─────────
1. Filter state via is_eligible() (excludes unhealthy and unknown).
2. Sort filtered set by backend_id (lexicographic) for a stable slot order.
3. Find the lowest backend_id strictly greater than _last_id; that's the next
   head of the rotation. (Falls back to the first eligible backend when
   _last_id is None or every id is ≤ _last_id.)
4. Assign descending scores: rank 0 → 1.0, rank 1 → (N-1)/N, ..., rank N-1 → 1/N.
5. Remember the head's backend_id as _last_id for the next call.

Why backend_id pointer, not modular index
─────────────────────────────────────────
The eligible set changes as backends go degraded/unknown/unhealthy. A modular
index (self._idx % len(eligible)) reshuffles the rotation whenever the set
size changes — adding or removing one backend perturbs every position. A
backend_id pointer is stable: removing the just-served backend still serves
the next id; adding a backend doesn't reset the cycle.

All-unhealthy / no-eligible fallback: policy_base._routing_fallback().
Degraded backends are NOT skipped; they are slower but still serving traffic.
Unknown backends are excluded (no telemetry — don't blind-route to them).
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


class RoundRobinPolicy(RoutingPolicy):
    """Cycles through eligible backends in sorted backend_id order.

    The rotation pointer is a backend_id, not a numeric index — so changes
    to the eligible set don't perturb the cycle.
    """

    def __init__(self) -> None:
        self._last_id: str | None = None

    def act(self, state: list[BackendState]) -> RoutingAction:
        eligible = sorted(
            [b for b in state if is_eligible(b.health)],
            key=lambda b: b.backend_id,
        )
        if not eligible:
            return _routing_fallback(state)

        # Pick the head: the lowest backend_id strictly greater than _last_id.
        # Wraps around to the first when _last_id is None or already past
        # the highest id.
        head_idx = 0
        if self._last_id is not None:
            for i, b in enumerate(eligible):
                if b.backend_id > self._last_id:
                    head_idx = i
                    break
            else:
                head_idx = 0   # wrap

        ordered = eligible[head_idx:] + eligible[:head_idx]
        self._last_id = ordered[0].backend_id

        n = len(ordered)
        rankings = [
            Ranking(backend_id=b.backend_id, score=(n - i) / n)
            for i, b in enumerate(ordered)
        ]
        return RoutingAction(mode="shadow", rankings=rankings)
