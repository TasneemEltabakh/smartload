"""Unit tests for MonotonePolicy."""

import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from policy_base import BackendState  # noqa: E402
from policies.monotone.policy import MonotonePolicy  # noqa: E402


def _b(backend_id, latency_ms, queue_depth=0, health="healthy"):
    return BackendState(backend_id=backend_id, latency_ms=latency_ms,
                        queue_depth=queue_depth, health=health)


def _policy(operating_mode="shadow"):
    # Force built-in config defaults — point at a path with no params.json so
    # __init__ falls back gracefully and the test stays artifact-independent.
    return MonotonePolicy(operating_mode=operating_mode,
                          model_path=str(_SERVICE_ROOT / "models" / "_does_not_exist"))


def _preferred(action):
    """The backend the policy prefers — the max-weighted ranking."""
    return max(action.rankings, key=lambda r: r.score).backend_id


def test_default_mode_is_shadow():
    assert _policy().act([_b("b0", 10.0), _b("b1", 20.0)]).mode == "shadow"


def test_hybrid_mode_is_active():
    assert _policy("hybrid").act([_b("b0", 10.0), _b("b1", 20.0)]).mode == "active"


def test_learning_mode_maps_to_active():
    assert _policy("learning").act([_b("b0", 10.0), _b("b1", 20.0)]).mode == "active"


def test_lower_latency_is_preferred():
    action = _policy().act([_b("b0", 10.0), _b("b1", 25.0)])
    assert _preferred(action) == "b0"


def test_weight_is_monotone_in_latency():
    # Lower-latency eligible backend never gets less weight than a slower one.
    action = _policy().act([_b("b0", 10.0), _b("b1", 15.0), _b("b2", 25.0)])
    by_id = {r.backend_id: r.score for r in action.rankings}
    assert by_id["b0"] >= by_id["b1"] >= by_id["b2"]


def test_excludes_unhealthy_and_unknown():
    state = [_b("b0", 10.0), _b("bx", 10.0, health="unhealthy"),
             _b("bu", 10.0, health="unknown")]
    assert {r.backend_id for r in _policy().act(state).rankings} == {"b0"}


def test_all_unhealthy_returns_empty_shadow():
    action = _policy().act([_b("b0", 10.0, health="unhealthy"),
                            _b("b1", 10.0, health="unhealthy")])
    assert action.mode == "shadow"
    assert action.rankings == []


def test_empty_state_returns_empty_shadow():
    action = _policy().act([])
    assert action.mode == "shadow"
    assert action.rankings == []


def test_weights_normalised_to_unit_interval():
    action = _policy().act([_b("b0", 10.0), _b("b1", 12.0), _b("b2", 14.0)])
    total = sum(r.score for r in action.rankings)
    assert abs(total - 1.0) < 1e-9
    assert all(0.0 < r.score <= 1.0 for r in action.rankings)
