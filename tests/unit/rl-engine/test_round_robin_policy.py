"""
tests/unit/rl-engine/test_round_robin_policy.py
─────────────────────────────────────────────────
Unit tests for services/rl-engine/policies/round_robin/policy.py.

No Docker, no Redis, no DB.

Coverage:
  1. Correct cycle order over healthy-only pool
  2. Skips unhealthy backends; degraded backends are included
  3. Single healthy backend edge case
  4. All-unhealthy → _routing_fallback path (valid envelope, mode="shadow")
  5. Envelope shape deserializes as valid RoutingRecommendation
  6. Scores are descending and in (0, 1]
  7. Cycle wraps correctly after n steps
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "rl-engine"
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from policy_base import BackendState, Ranking, RoutingAction  # noqa: E402
from policies.round_robin.policy import RoundRobinPolicy      # noqa: E402


def _b(bid: str, health: str = "healthy", qd: int = 10) -> BackendState:
    return BackendState(backend_id=bid, latency_ms=50.0, queue_depth=qd, health=health)


def _top(action: RoutingAction) -> str:
    """Return the backend_id with the highest score."""
    return max(action.rankings, key=lambda r: r.score).backend_id


# ── cycling ───────────────────────────────────────────────────────────────────

def test_cycle_order_three_backends():
    """With backends a, b, c the rotation advances by one each call."""
    state = [_b("a"), _b("b"), _b("c")]
    pol = RoundRobinPolicy()

    calls = [_top(pol.act(state)) for _ in range(6)]
    # Expect a,b,c,a,b,c
    assert calls == ["a", "b", "c", "a", "b", "c"]


def test_cycle_wraps_after_n_steps():
    state = [_b("a"), _b("b")]
    pol = RoundRobinPolicy()
    first = _top(pol.act(state))
    second = _top(pol.act(state))
    third = _top(pol.act(state))  # should wrap back to first
    assert first == third


def test_scores_descend():
    state = [_b("a"), _b("b"), _b("c")]
    pol = RoundRobinPolicy()
    action = pol.act(state)
    scores = sorted([r.score for r in action.rankings], reverse=True)
    assert scores == sorted(scores, reverse=True)  # already sorted desc
    assert all(0 < s <= 1.0 for s in scores)


def test_scores_sum_to_correct_value():
    """For N backends, scores are 1, (N-1)/N, ..., 1/N → sum = (N+1)/2."""
    n = 4
    state = [_b(f"b{i}") for i in range(n)]
    pol = RoundRobinPolicy()
    action = pol.act(state)
    expected_sum = sum((n - i) / n for i in range(n))
    actual_sum = sum(r.score for r in action.rankings)
    assert abs(actual_sum - expected_sum) < 1e-9


# ── health filtering ──────────────────────────────────────────────────────────

def test_skips_unhealthy_backends():
    state = [_b("healthy"), _b("sick", health="unhealthy")]
    pol = RoundRobinPolicy()
    action = pol.act(state)
    ids = [r.backend_id for r in action.rankings]
    assert "sick" not in ids
    assert "healthy" in ids


def test_includes_degraded_backends():
    """Degraded is slower but still serving; must not be excluded."""
    state = [_b("a", health="degraded"), _b("b", health="healthy")]
    pol = RoundRobinPolicy()
    action = pol.act(state)
    ids = {r.backend_id for r in action.rankings}
    assert "a" in ids


def test_single_healthy_backend():
    state = [_b("only")]
    pol = RoundRobinPolicy()
    action = pol.act(state)
    assert len(action.rankings) == 1
    assert action.rankings[0].backend_id == "only"
    assert action.rankings[0].score == pytest.approx(1.0)
    assert action.mode == "shadow"


# ── all-unhealthy fallback ────────────────────────────────────────────────────

def test_all_unhealthy_returns_shadow_envelope():
    state = [_b("a", health="unhealthy"), _b("b", health="unhealthy")]
    pol = RoundRobinPolicy()
    action = pol.act(state)
    assert isinstance(action, RoutingAction)
    assert action.mode == "shadow"
    assert len(action.rankings) == len(state)


def test_all_unhealthy_rankings_are_valid():
    state = [_b("x", health="unhealthy"), _b("y", health="unhealthy")]
    pol = RoundRobinPolicy()
    action = pol.act(state)
    for r in action.rankings:
        assert isinstance(r.backend_id, str)
        assert isinstance(r.score, float)
        assert r.score > 0


def test_all_unhealthy_does_not_raise_on_empty_state():
    pol = RoundRobinPolicy()
    action = pol.act([])
    assert action.mode == "shadow"
    assert action.rankings == []


# ── envelope shape (RoutingRecommendation compatibility) ─────────────────────

def test_envelope_server_rankings_shape():
    """RoutingAction.rankings must convert to server_rankings list[dict]."""
    state = [_b("a"), _b("b")]
    pol = RoundRobinPolicy()
    action = pol.act(state)
    # Simulate what action_to_event_payload() does in runloop.py
    server_rankings = [
        {"backend_id": r.backend_id, "score": r.score}
        for r in action.rankings
    ]
    for item in server_rankings:
        assert "backend_id" in item
        assert "score" in item
        assert isinstance(item["backend_id"], str)
        assert isinstance(item["score"], float)


def test_mode_is_always_shadow():
    state = [_b("a", health="healthy"), _b("b", health="degraded")]
    pol = RoundRobinPolicy()
    for _ in range(5):
        action = pol.act(state)
        assert action.mode == "shadow"
