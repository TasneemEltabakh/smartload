"""
tests/unit/rl-engine/test_least_connections_policy.py
──────────────────────────────────────────────────────
Unit tests for services/rl-engine/policies/least_connections/policy.py.

No Docker, no Redis, no DB.

Coverage:
  1. Selects backend with lowest queue_depth among healthy backends
  2. Skips unhealthy regardless of queue_depth
  3. Degraded backends are included
  4. Tie-breaking: lower backend_id wins, deterministic across calls
  5. All-unhealthy → same canonical fallback as round_robin
  6. Scores are descending and in (0, 1]
  7. Envelope shape matches RoutingRecommendation
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "rl-engine"
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from policy_base import BackendState, RoutingAction              # noqa: E402
from policies.least_connections.policy import LeastConnectionsPolicy  # noqa: E402


def _b(bid: str, qd: int = 10, health: str = "healthy") -> BackendState:
    return BackendState(backend_id=bid, latency_ms=50.0, queue_depth=qd, health=health)


def _top(action: RoutingAction) -> str:
    return max(action.rankings, key=lambda r: r.score).backend_id


# ── core ordering ─────────────────────────────────────────────────────────────

def test_selects_lowest_queue_depth():
    state = [_b("high", qd=100), _b("low", qd=5), _b("mid", qd=50)]
    pol = LeastConnectionsPolicy()
    assert _top(pol.act(state)) == "low"


def test_ordering_reflects_ascending_queue_depth():
    state = [_b("c", qd=30), _b("a", qd=10), _b("b", qd=20)]
    pol = LeastConnectionsPolicy()
    action = pol.act(state)
    ordered_ids = [r.backend_id for r in sorted(action.rankings, key=lambda r: -r.score)]
    assert ordered_ids == ["a", "b", "c"]


def test_scores_descend():
    state = [_b("a", qd=10), _b("b", qd=20), _b("c", qd=30)]
    pol = LeastConnectionsPolicy()
    action = pol.act(state)
    scores = [r.score for r in sorted(action.rankings, key=lambda r: -r.score)]
    assert scores == sorted(scores, reverse=True)
    assert all(0 < s <= 1.0 for s in scores)


# ── health filtering ──────────────────────────────────────────────────────────

def test_skips_unhealthy_regardless_of_queue_depth():
    """Unhealthy backend with queue_depth=0 must still be excluded."""
    state = [
        _b("sick",    qd=0,  health="unhealthy"),
        _b("healthy", qd=50, health="healthy"),
    ]
    pol = LeastConnectionsPolicy()
    action = pol.act(state)
    ids = [r.backend_id for r in action.rankings]
    assert "sick" not in ids
    assert "healthy" in ids


def test_includes_degraded_backends():
    """Degraded is slower but still serving; must not be excluded."""
    state = [_b("d", qd=5, health="degraded"), _b("h", qd=10, health="healthy")]
    pol = LeastConnectionsPolicy()
    action = pol.act(state)
    ids = {r.backend_id for r in action.rankings}
    assert "d" in ids


# ── tie-breaking ──────────────────────────────────────────────────────────────

def test_tie_broken_by_backend_id_ascending():
    """Two backends with equal queue_depth → lower backend_id wins."""
    state = [_b("z", qd=10), _b("a", qd=10)]
    pol = LeastConnectionsPolicy()
    assert _top(pol.act(state)) == "a"


def test_tie_breaking_is_deterministic():
    """Same state → same result on every call (no randomness)."""
    state = [_b("z", qd=10), _b("a", qd=10), _b("m", qd=10)]
    pol = LeastConnectionsPolicy()
    results = [_top(pol.act(state)) for _ in range(10)]
    assert len(set(results)) == 1, "tie-breaking must be deterministic"


# ── all-unhealthy fallback ────────────────────────────────────────────────────

def test_all_unhealthy_returns_shadow_envelope():
    state = [_b("a", health="unhealthy"), _b("b", health="unhealthy")]
    pol = LeastConnectionsPolicy()
    action = pol.act(state)
    assert isinstance(action, RoutingAction)
    assert action.mode == "shadow"
    assert len(action.rankings) == len(state)


def test_all_unhealthy_fallback_consistent_with_round_robin():
    """Both policies use the same _routing_fallback — results must match."""
    from policies.round_robin.policy import RoundRobinPolicy
    state = [_b("a", health="unhealthy"), _b("b", health="unhealthy")]
    lc_action = LeastConnectionsPolicy().act(state)
    rr_action  = RoundRobinPolicy().act(state)
    # Both call _routing_fallback → same uniform-score output
    lc_scores = {r.backend_id: r.score for r in lc_action.rankings}
    rr_scores  = {r.backend_id: r.score for r in rr_action.rankings}
    assert lc_scores == rr_scores


def test_all_unhealthy_empty_state_no_raise():
    pol = LeastConnectionsPolicy()
    action = pol.act([])
    assert action.mode == "shadow"
    assert action.rankings == []


# ── envelope shape ────────────────────────────────────────────────────────────

def test_envelope_server_rankings_shape():
    state = [_b("a", qd=5), _b("b", qd=10)]
    pol = LeastConnectionsPolicy()
    action = pol.act(state)
    server_rankings = [
        {"backend_id": r.backend_id, "score": r.score}
        for r in action.rankings
    ]
    for item in server_rankings:
        assert "backend_id" in item and "score" in item
        assert isinstance(item["backend_id"], str)
        assert isinstance(item["score"], float)


def test_mode_is_always_shadow():
    state = [_b("a"), _b("b")]
    pol = LeastConnectionsPolicy()
    for _ in range(5):
        assert pol.act(state).mode == "shadow"
