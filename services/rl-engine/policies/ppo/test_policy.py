"""Unit tests for PPOPolicy — the model-not-ready fallback path.

These exercise PPOPolicy WITHOUT a trained artifact: constructed against a
nonexistent model_path it sets policy_ready=False and act() returns uniform
shadow rankings, so the suite needs neither a policy.zip nor torch /
stable-baselines3 (those are imported lazily, only on the inference path).
The whole module skips cleanly if the import chain is unavailable.
"""

import sys
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

try:
    from policy_base import BackendState  # noqa: E402
    from policies.ppo.policy import PPOPolicy  # noqa: E402
except Exception as exc:  # pragma: no cover - environment guard
    pytest.skip(f"ppo policy import unavailable: {exc}", allow_module_level=True)


def _state(ids, health="healthy"):
    return [
        BackendState(backend_id=i, latency_ms=10.0, queue_depth=0, health=health)
        for i in ids
    ]


def _unready(tmp_path, **kwargs):
    """A PPOPolicy pointed at a nonexistent artifact -> fallback mode."""
    return PPOPolicy(model_path=str(tmp_path / "no_such_model"), **kwargs)


def test_missing_artifact_is_not_ready(tmp_path):
    assert _unready(tmp_path).policy_ready is False


def test_fallback_is_uniform_shadow_over_eligible(tmp_path):
    action = _unready(tmp_path).act(_state(["b0", "b1", "b2"]))
    assert action.mode == "shadow"
    assert {r.backend_id for r in action.rankings} == {"b0", "b1", "b2"}
    scores = [r.score for r in action.rankings]
    assert len(set(scores)) == 1 and abs(scores[0] - 1 / 3) < 1e-9


def test_fallback_excludes_unhealthy(tmp_path):
    state = _state(["b0", "b1"]) + _state(["bx"], health="unhealthy")
    ids = {r.backend_id for r in _unready(tmp_path).act(state).rankings}
    assert ids == {"b0", "b1"}


def test_empty_state_returns_empty_rankings(tmp_path):
    assert _unready(tmp_path).act([]).rankings == []


def test_reload_updates_operating_mode(tmp_path):
    p = _unready(tmp_path, operating_mode="shadow")
    p.reload(operating_mode="hybrid")
    assert p._operating_mode == "hybrid"
    p.reload(operating_mode="learning")  # legacy alias -> hybrid
    assert p._operating_mode == "hybrid"


def test_reload_ignores_unknown_kwargs(tmp_path):
    p = _unready(tmp_path)
    p.reload(some_future_knob=123)  # silently ignored, no raise
    assert p.policy_ready is False
