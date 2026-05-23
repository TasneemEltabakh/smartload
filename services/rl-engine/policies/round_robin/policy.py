"""
services/rl-engine/policies/round_robin/policy.py
──────────────────────────────────────────────────
RoundRobinPolicy — stateful cyclic scheduler over healthy/degraded backends.

Algorithm
─────────
1. Filter state to backends where health != "unhealthy".
2. Sort filtered set by backend_id (lexicographic) for a stable slot order.
3. Rotate the sorted list so the backend at position _idx is first.
4. Assign descending scores: rank 0 → 1.0, rank 1 → (N-1)/N, ..., rank N-1 → 1/N.
5. Increment _idx modulo N.
6. Always returns mode="shadow".

All-unhealthy fallback: policy_base._routing_fallback() — see its docstring.
Degraded backends are NOT skipped; they are slower but still serving traffic.
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
)


class RoundRobinPolicy(RoutingPolicy):
    """Cycles through healthy/degraded backends in sorted backend_id order."""

    def __init__(self) -> None:
        self._idx: int = 0

    def act(self, state: list[BackendState]) -> RoutingAction:
        eligible = sorted(
            [b for b in state if b.health != "unhealthy"],
            key=lambda b: b.backend_id,
        )
        if not eligible:
            return _routing_fallback(state)

        n = len(eligible)
        # Rotate: start from _idx, wrap around
        ordered = eligible[self._idx % n:] + eligible[: self._idx % n]
        self._idx = (self._idx + 1) % n

        rankings = [
            Ranking(backend_id=b.backend_id, score=(n - i) / n)
            for i, b in enumerate(ordered)
        ]
        return RoutingAction(mode="shadow", rankings=rankings)
