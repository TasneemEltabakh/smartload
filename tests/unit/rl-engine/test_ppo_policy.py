"""
tests/unit/rl-engine/test_ppo_policy.py
────────────────────────────────────────
Unit tests for services/rl-engine/policies/ppo/policy.py.

No Docker, no Redis, no DB.  Uses the pre-generated fixture zip at
tests/fixtures/rl-engine/policy.zip + artifact_meta.json.

The fixture was created with a 256-step MaskablePPO warm-up on a single
Alibaba partition.  Its weights are essentially random; tests only verify
the *interface contract*, not the quality of routing decisions.

Coverage:
  1. test_ppo_loads_fixture         — fixture zip + matching meta → policy_ready=True
  2. test_shadow_mode_inference     — operating_mode="shadow" → mode="shadow",
                                      non-empty rankings, no unhealthy backends
  3. test_hybrid_mode_active        — operating_mode="hybrid" → mode="active"
  4. test_learning_mode_active      — operating_mode="learning" → mode="active"
  5. test_unhealthy_absent          — unhealthy backend never appears in rankings
  6. test_all_unhealthy_rankings    — all-unhealthy state → mode="shadow",
                                      at least one ranking returned
  7. test_missing_zip_not_ready     — meta exists but zip absent → policy_ready=False,
                                      act() still returns valid shadow rankings
  8. test_nbmax_mismatch_raises     — meta with wrong n_max_backends → ValueError
  9. test_reload_raises             — reload() raises NotImplementedError
 10. test_scores_are_valid          — scores in (0, 1], sum to ~1.0
 11. test_rankings_descend          — rankings are sorted high-to-low score
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "rl-engine"
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from policy_base import BackendState, RoutingAction   # noqa: E402

# Fixture directory with pre-built MaskablePPO artifact (256 training steps)
_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "rl-engine"

# Skip all tests in this file if sb3-contrib is not installed
pytest.importorskip("sb3_contrib", reason="sb3_contrib not installed — skip PPO tests")


# ── helpers ───────────────────────────────────────────────────────────────────

def _b(bid: str, health: str = "healthy", latency: float = 50.0, qd: int = 10) -> BackendState:
    return BackendState(backend_id=bid, latency_ms=latency, queue_depth=qd, health=health)


def _load_ppo(operating_mode: str = "shadow", model_path: str | None = None):
    from policies.ppo.policy import PPOPolicy
    if model_path is None:
        model_path = str(_FIXTURE_DIR / "policy")
    return PPOPolicy(operating_mode=operating_mode, model_path=model_path)


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ppo_shadow():
    """Single shared instance for shadow-mode tests (expensive to load)."""
    return _load_ppo("shadow")


@pytest.fixture(scope="module")
def ppo_hybrid():
    return _load_ppo("hybrid")


@pytest.fixture(scope="module")
def ppo_learning():
    return _load_ppo("learning")


_MIXED_STATE = [
    _b("backend_1", health="healthy",   latency=40.0, qd=20),
    _b("backend_2", health="degraded",  latency=250.0, qd=5),
    _b("backend_3", health="unhealthy", latency=10.0,  qd=0),
    _b("backend_4", health="healthy",   latency=60.0, qd=15),
    _b("backend_5", health="healthy",   latency=55.0, qd=12),
]


# ── 1. loading ────────────────────────────────────────────────────────────────

def test_ppo_loads_fixture(ppo_shadow):
    assert ppo_shadow.policy_ready is True


# ── 2. shadow mode ────────────────────────────────────────────────────────────

def test_shadow_mode_returns_shadow(ppo_shadow):
    action = ppo_shadow.act(_MIXED_STATE)
    assert action.mode == "shadow"


def test_shadow_mode_has_rankings(ppo_shadow):
    action = ppo_shadow.act(_MIXED_STATE)
    assert len(action.rankings) >= 1


def test_shadow_mode_no_unhealthy_in_rankings(ppo_shadow):
    action = ppo_shadow.act(_MIXED_STATE)
    ids = {r.backend_id for r in action.rankings}
    assert "backend_3" not in ids, "unhealthy backend must not appear in rankings"


# ── 3 & 4. active modes ───────────────────────────────────────────────────────

def test_hybrid_mode_returns_active(ppo_hybrid):
    action = ppo_hybrid.act(_MIXED_STATE)
    assert action.mode == "active"


def test_learning_mode_returns_active(ppo_learning):
    action = ppo_learning.act(_MIXED_STATE)
    assert action.mode == "active"


# ── 5. unhealthy always excluded ──────────────────────────────────────────────

def test_unhealthy_absent_all_modes():
    """No mode should include an unhealthy backend in server_rankings."""
    for mode in ("shadow", "hybrid", "learning"):
        pol = _load_ppo(mode)
        action = pol.act(_MIXED_STATE)
        ids = {r.backend_id for r in action.rankings}
        assert "backend_3" not in ids, f"unhealthy in rankings for mode={mode}"


# ── 6. all-unhealthy state ────────────────────────────────────────────────────

def test_all_unhealthy_returns_empty_shadow_rankings(ppo_shadow):
    """When every live backend is unhealthy, emit empty rankings and
    mode=shadow. Anomaly detector's existing sidecar exclusions stay in
    effect; we do NOT manufacture a "best of the bad" pick. See M4 in
    the RL review."""
    state = [
        _b("backend_1", health="unhealthy"),
        _b("backend_2", health="unhealthy"),
    ]
    action = ppo_shadow.act(state)
    assert isinstance(action, RoutingAction)
    assert action.mode == "shadow"
    assert action.rankings == []


def test_all_unknown_returns_empty_shadow_rankings(ppo_shadow):
    """Same contract for "unknown" health — silent backends are excluded."""
    state = [
        _b("backend_1", health="unknown"),
        _b("backend_2", health="unknown"),
    ]
    action = ppo_shadow.act(state)
    assert action.mode == "shadow"
    assert action.rankings == []


# ── 7. missing zip — graceful not-ready ───────────────────────────────────────

def test_missing_zip_policy_not_ready(tmp_path):
    """Meta exists but zip is absent → policy_ready=False, no crash."""
    meta = json.loads((_FIXTURE_DIR / "artifact_meta.json").read_text())
    (tmp_path / "artifact_meta.json").write_text(json.dumps(meta))
    # policy.zip intentionally not copied

    from policies.ppo.policy import PPOPolicy
    pol = PPOPolicy(model_path=str(tmp_path / "policy"))
    assert pol.policy_ready is False

    # act() must still return a valid shadow RoutingAction
    action = pol.act([_b("backend_1")])
    assert isinstance(action, RoutingAction)
    assert action.mode == "shadow"
    assert len(action.rankings) >= 1


# ── 8. n_max_backends mismatch ────────────────────────────────────────────────

def test_nbmax_mismatch_raises(tmp_path):
    """artifact_meta.json with wrong n_max_backends → ValueError on init."""
    import shutil
    shutil.copy(_FIXTURE_DIR / "policy.zip", tmp_path / "policy.zip")

    bad_meta = json.loads((_FIXTURE_DIR / "artifact_meta.json").read_text())
    bad_meta["n_max_backends"] = 99   # deliberately wrong
    (tmp_path / "artifact_meta.json").write_text(json.dumps(bad_meta))

    from policies.ppo.policy import PPOPolicy
    with pytest.raises(ValueError, match="n_max_backends"):
        PPOPolicy(model_path=str(tmp_path / "policy"))


# ── 9. reload() raises ────────────────────────────────────────────────────────

def test_reload_updates_runtime_kwargs_in_place(ppo_shadow):
    """C4: reload() updates operating_mode without reloading policy.zip.

    The PPOPolicy instance is shared at module scope (expensive to load),
    so this test snapshots and restores the mutated state on teardown.
    """
    original_mode = ppo_shadow._operating_mode
    try:
        ppo_shadow.reload(operating_mode="hybrid",
                          confidence_threshold=0.81,
                          exploration_rate=0.12)
        assert ppo_shadow._operating_mode == "hybrid"
        assert ppo_shadow._confidence_threshold == 0.81
        assert ppo_shadow._exploration_rate == 0.12
        # The act() path now reflects hybrid even though we never
        # reconstructed the policy from disk.
        action = ppo_shadow.act(_MIXED_STATE)
        assert action.mode == "active"
    finally:
        ppo_shadow.reload(operating_mode=original_mode)


def test_reload_ignores_unknown_kwargs(ppo_shadow):
    """Unknown kwargs are silently ignored so the policy plugin can
    evolve independently of the engine_policy payload shape."""
    original_mode = ppo_shadow._operating_mode
    try:
        ppo_shadow.reload(operating_mode="shadow", future_field="value")
        assert ppo_shadow._operating_mode == "shadow"
    finally:
        ppo_shadow.reload(operating_mode=original_mode)


def test_reload_learning_maps_to_hybrid(ppo_shadow):
    """Deprecated "learning" → "hybrid" inside the policy too, so old
    policy.yaml configs stay compatible after the M10 cleanup."""
    original_mode = ppo_shadow._operating_mode
    try:
        ppo_shadow.reload(operating_mode="learning")
        assert ppo_shadow._operating_mode == "hybrid"
    finally:
        ppo_shadow.reload(operating_mode=original_mode)


# ── 10. score validity ────────────────────────────────────────────────────────

def test_scores_are_in_range(ppo_shadow):
    action = ppo_shadow.act(_MIXED_STATE)
    for r in action.rankings:
        assert 0.0 < r.score <= 1.0, f"score {r.score} out of (0, 1] for {r.backend_id}"


def test_scores_sum_to_one(ppo_shadow):
    action = ppo_shadow.act(_MIXED_STATE)
    total = sum(r.score for r in action.rankings)
    assert abs(total - 1.0) < 1e-5, f"scores sum to {total}, expected ~1.0"


def test_argmax_dominant_weighting_shape(ppo_shadow):
    """C2: chosen backend gets _DOMINANT_WEIGHT (0.7); the remaining 0.3
    is split evenly across other eligibles. backend_3 is unhealthy in
    _MIXED_STATE so 4 eligibles → 0.7 + 3 * 0.1 = 1.0."""
    from policies.ppo.policy import _DOMINANT_WEIGHT
    action = ppo_shadow.act(_MIXED_STATE)
    assert len(action.rankings) == 4   # 5 live - 1 unhealthy
    assert action.rankings[0].score == pytest.approx(_DOMINANT_WEIGHT)
    expected_floor = (1.0 - _DOMINANT_WEIGHT) / 3
    for r in action.rankings[1:]:
        assert r.score == pytest.approx(expected_floor)


def test_argmax_dominant_single_eligible_gets_full_weight(ppo_shadow):
    """When only one backend is eligible, it receives score=1.0 — there is
    no "other" mass to distribute."""
    state = [
        _b("backend_1", health="healthy"),
        _b("backend_2", health="unhealthy"),
        _b("backend_3", health="unhealthy"),
    ]
    action = ppo_shadow.act(state)
    assert len(action.rankings) == 1
    assert action.rankings[0].backend_id == "backend_1"
    assert action.rankings[0].score == pytest.approx(1.0)


# ── 11. rankings descend ──────────────────────────────────────────────────────

def test_rankings_are_descending(ppo_shadow):
    action = ppo_shadow.act(_MIXED_STATE)
    scores = [r.score for r in action.rankings]
    assert scores == sorted(scores, reverse=True), "rankings must be sorted high→low"
