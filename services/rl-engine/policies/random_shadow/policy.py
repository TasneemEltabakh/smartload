"""Random-shadow policy. Validates the full pipeline before a trained PPO ships."""

from __future__ import annotations

import random
import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from policy_base import RoutingPolicy, RoutingAction, Ranking, BackendState  # noqa: E402


class RandomShadowPolicy(RoutingPolicy):
    """Assigns uniform-random scores to each backend.

    Always reports `mode=shadow`. The shadow output is logged but never
    affects live routing — it just proves the envelope round-trips.
    """

    def __init__(self, seed: int | None = None):
        self._rng = random.Random(seed)

    def act(self, state: list[BackendState]) -> RoutingAction:
        rankings = [
            Ranking(backend_id=b.backend_id, score=self._rng.random())
            for b in state
        ]
        return RoutingAction(mode="shadow", rankings=rankings)
