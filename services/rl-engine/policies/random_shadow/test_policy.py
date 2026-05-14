"""Unit tests for RandomShadowPolicy."""

import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from policy_base import BackendState  # noqa: E402
from policies.random_shadow.policy import RandomShadowPolicy  # noqa: E402


def _state(n: int = 3) -> list[BackendState]:
    return [
        BackendState(
            backend_id=f"b{i}",
            latency_ms=10.0,
            queue_depth=0,
            health="healthy",
        )
        for i in range(n)
    ]


def test_emits_shadow_mode():
    p = RandomShadowPolicy(seed=42)
    action = p.act(_state())
    assert action.mode == "shadow"


def test_one_ranking_per_backend():
    p = RandomShadowPolicy(seed=42)
    action = p.act(_state(5))
    assert len(action.rankings) == 5
    assert {r.backend_id for r in action.rankings} == {f"b{i}" for i in range(5)}


def test_scores_in_unit_interval():
    p = RandomShadowPolicy(seed=42)
    action = p.act(_state(3))
    assert all(0.0 <= r.score <= 1.0 for r in action.rankings)


def test_seeded_runs_are_reproducible():
    a = RandomShadowPolicy(seed=42).act(_state(3))
    b = RandomShadowPolicy(seed=42).act(_state(3))
    assert [r.score for r in a.rankings] == [r.score for r in b.rankings]
