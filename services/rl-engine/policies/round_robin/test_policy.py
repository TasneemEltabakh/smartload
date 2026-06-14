"""Unit tests for RoundRobinPolicy."""

import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from policy_base import BackendState  # noqa: E402
from policies.round_robin.policy import RoundRobinPolicy  # noqa: E402


def _state(ids, health="healthy"):
    return [
        BackendState(backend_id=i, latency_ms=10.0, queue_depth=0, health=health)
        for i in ids
    ]


def _head(action):
    """The backend the rotation currently points at — the max-scored ranking."""
    return max(action.rankings, key=lambda r: r.score).backend_id


def test_emits_shadow_mode():
    assert RoundRobinPolicy().act(_state(["b0", "b1", "b2"])).mode == "shadow"


def test_one_ranking_per_eligible_backend():
    action = RoundRobinPolicy().act(_state(["b0", "b1", "b2"]))
    assert {r.backend_id for r in action.rankings} == {"b0", "b1", "b2"}


def test_excludes_unhealthy_and_unknown():
    state = _state(["b0", "b1"]) + _state(["bx"], health="unhealthy") + _state(["bu"], health="unknown")
    ids = {r.backend_id for r in RoundRobinPolicy().act(state).rankings}
    assert ids == {"b0", "b1"}


def test_scores_in_unit_interval():
    action = RoundRobinPolicy().act(_state(["b0", "b1", "b2"]))
    assert all(0.0 < r.score <= 1.0 for r in action.rankings)


def test_rotation_advances_by_backend_id():
    p = RoundRobinPolicy()
    first = _head(p.act(_state(["b0", "b1", "b2"])))   # _last_id None -> lowest id
    second = _head(p.act(_state(["b0", "b1", "b2"])))  # next id strictly greater
    third = _head(p.act(_state(["b0", "b1", "b2"])))
    assert (first, second, third) == ("b0", "b1", "b2")


def test_rotation_wraps_after_highest():
    p = RoundRobinPolicy()
    for _ in range(3):
        last = _head(p.act(_state(["b0", "b1", "b2"])))
    assert last == "b2"
    assert _head(p.act(_state(["b0", "b1", "b2"]))) == "b0"  # wrap


def test_pointer_stable_when_eligible_set_shrinks():
    # Removing a just-served backend still advances to the next id (the reason
    # the policy tracks a backend_id pointer, not a modular index).
    p = RoundRobinPolicy()
    assert _head(p.act(_state(["b0", "b1", "b2"]))) == "b0"
    assert _head(p.act(_state(["b1", "b2"]))) == "b1"


def test_all_unhealthy_falls_back_to_uniform_shadow():
    action = RoundRobinPolicy().act(_state(["b0", "b1"], health="unhealthy"))
    assert action.mode == "shadow"
    scores = [r.score for r in action.rankings]
    assert len(scores) == 2 and len(set(scores)) == 1  # uniform
