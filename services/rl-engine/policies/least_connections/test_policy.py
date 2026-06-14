"""Unit tests for LeastConnectionsPolicy."""

import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from policy_base import BackendState  # noqa: E402
from policies.least_connections.policy import LeastConnectionsPolicy  # noqa: E402


def _b(backend_id, queue_depth, health="healthy"):
    return BackendState(backend_id=backend_id, latency_ms=10.0,
                        queue_depth=queue_depth, health=health)


def _preferred(action):
    """The backend the policy prefers — the max-scored ranking."""
    return max(action.rankings, key=lambda r: r.score).backend_id


def test_emits_shadow_mode():
    assert LeastConnectionsPolicy().act([_b("b0", 0), _b("b1", 5)]).mode == "shadow"


def test_lowest_queue_depth_is_preferred():
    action = LeastConnectionsPolicy().act([_b("b0", 9), _b("b1", 2), _b("b2", 5)])
    assert _preferred(action) == "b1"


def test_scores_strictly_descending_with_load():
    action = LeastConnectionsPolicy().act([_b("b0", 1), _b("b1", 2), _b("b2", 3)])
    by_id = {r.backend_id: r.score for r in action.rankings}
    assert by_id["b0"] > by_id["b1"] > by_id["b2"]


def test_tie_break_by_backend_id():
    # Equal load -> lower backend_id wins (deterministic).
    action = LeastConnectionsPolicy().act([_b("b1", 4), _b("b0", 4)])
    assert _preferred(action) == "b0"


def test_excludes_unhealthy_and_unknown():
    state = [_b("b0", 1), _b("bx", 0, health="unhealthy"), _b("bu", 0, health="unknown")]
    assert {r.backend_id for r in LeastConnectionsPolicy().act(state).rankings} == {"b0"}


def test_scores_in_unit_interval():
    action = LeastConnectionsPolicy().act([_b("b0", 0), _b("b1", 1), _b("b2", 2)])
    assert all(0.0 < r.score <= 1.0 for r in action.rankings)


def test_all_unhealthy_falls_back_to_uniform_shadow():
    action = LeastConnectionsPolicy().act([_b("b0", 0, health="unhealthy"),
                                           _b("b1", 0, health="unhealthy")])
    assert action.mode == "shadow"
    scores = [r.score for r in action.rankings]
    assert len(scores) == 2 and len(set(scores)) == 1
