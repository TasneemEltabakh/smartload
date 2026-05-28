"""
tests/unit/lb-sidecar/test_runloop.py
──────────────────────────────────────
Pure-Python unit tests for services/lb-sidecar/runloop.py.

No Docker, no Redis, no filesystem — runs in the unit-tests CI job.

Coverage:
  1. scores_to_weights — score→weight mapping, zero-score floor, empty list.
  2. BackendRegistry.translate — happy path, unmapped IP triggers refresh,
                                 hostname passthrough.
  3. BackendRegistry.translate_one — happy path, refresh on miss.
  4. handle_routing — shadow no-op, active applies weights, adapter error captured.
  5. handle_anomaly — unhealthy→exclude, healthy→include, error captured.
  6. handle_policy — safe_mode=True reverts to equal weights, False is no-op.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_SERVICE = Path(__file__).resolve().parents[3] / "services" / "lb-sidecar"
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from runloop import (  # noqa: E402
    BackendRegistry,
    AnomalyOutcome,
    KNOWN_BACKEND_FLOOR_WEIGHT,
    PolicyOutcome,
    PolicyState,
    RoutingOutcome,
    handle_anomaly,
    handle_policy,
    handle_routing,
    scores_to_weights,
)


# ── scores_to_weights ─────────────────────────────────────────────────────────

def test_scores_to_weights_basic():
    rankings = [
        {"backend_id": "b1:8080", "score": 0.9},
        {"backend_id": "b2:8080", "score": 0.1},
    ]
    result = scores_to_weights(rankings)
    assert result == {"b1:8080": 90, "b2:8080": 10}


def test_scores_to_weights_floors_at_one():
    rankings = [{"backend_id": "b:8080", "score": 0.004}]
    result = scores_to_weights(rankings)
    assert result["b:8080"] == 1


def test_scores_to_weights_empty():
    assert scores_to_weights([]) == {}


def test_scores_to_weights_full_score():
    rankings = [{"backend_id": "b:8080", "score": 1.0}]
    assert scores_to_weights(rankings) == {"b:8080": 100}


# ── BackendRegistry ───────────────────────────────────────────────────────────

def _make_mock_container(name: str, ip: str, port: str = "8080"):
    c = MagicMock()
    c.name = name
    c.attrs = {
        "NetworkSettings": {
            "Networks": {"smartload-net": {"IPAddress": ip}},
            "Ports": {f"{port}/tcp": [{"HostPort": port}]},
        }
    }
    return c


def _make_docker(containers):
    docker = MagicMock()
    docker.containers.list.return_value = containers
    return docker


def test_registry_translate_ip_to_name():
    c = _make_mock_container("smartload-test-backend-1", "172.18.0.5")
    registry = BackendRegistry(_make_docker([c]))
    result = registry.translate({"172.18.0.5:8080": 80})
    assert result == {"smartload-test-backend-1:8080": 80}


def test_registry_translate_hostname_passthrough():
    registry = BackendRegistry(_make_docker([]))
    result = registry.translate({"smartload-test-backend-1:8080": 50})
    assert result == {"smartload-test-backend-1:8080": 50}


def test_registry_translate_triggers_refresh_on_unknown_ip():
    c1 = _make_mock_container("backend-1", "10.0.0.1")
    c2 = _make_mock_container("backend-2", "10.0.0.2")
    docker = _make_docker([c1])
    registry = BackendRegistry(docker)

    # After initial build only 10.0.0.1 is known; 10.0.0.2 is unknown.
    # Mutate the docker mock so refresh picks up c2.
    docker.containers.list.return_value = [c1, c2]
    result = registry.translate({"10.0.0.2:8080": 60})
    assert result == {"backend-2:8080": 60}


def test_registry_translate_one_hit():
    c = _make_mock_container("backend-3", "192.168.1.3")
    registry = BackendRegistry(_make_docker([c]))
    assert registry.translate_one("192.168.1.3:8080") == "backend-3:8080"


def test_registry_translate_one_miss_refreshes():
    c = _make_mock_container("backend-4", "192.168.1.4")
    docker = _make_docker([])
    registry = BackendRegistry(docker)
    docker.containers.list.return_value = [c]
    assert registry.translate_one("192.168.1.4:8080") == "backend-4:8080"


def test_registry_docker_none_is_safe():
    registry = BackendRegistry(None)
    result = registry.translate({"b:8080": 10})
    assert result == {"b:8080": 10}


# ── handle_routing ────────────────────────────────────────────────────────────

def _stub_registry():
    r = MagicMock(spec=BackendRegistry)
    r.translate.side_effect = lambda w: w  # identity — keys unchanged
    return r


def test_handle_routing_shadow_is_noop():
    adapter = MagicMock()
    registry = _stub_registry()
    outcome = handle_routing(
        {"mode": "shadow", "server_rankings": [{"backend_id": "b:8080", "score": 0.5}]},
        registry, adapter, ["b:8080"],
    )
    assert outcome.applied is False
    assert outcome.mode == "shadow"
    adapter.set_upstream_weights.assert_not_called()


def test_handle_routing_active_applies_weights():
    adapter = MagicMock()
    registry = _stub_registry()
    outcome = handle_routing(
        {"mode": "active", "server_rankings": [
            {"backend_id": "b1:8080", "score": 0.8},
            {"backend_id": "b2:8080", "score": 0.6},
        ]},
        registry, adapter, ["b1:8080", "b2:8080"],
    )
    assert outcome.applied is True
    assert outcome.weight_count == 2
    assert outcome.confidence == 0.8
    adapter.set_upstream_weights.assert_called_once()
    applied = adapter.set_upstream_weights.call_args[0][0]
    assert applied == {"b1:8080": 80, "b2:8080": 60}


def test_handle_routing_empty_rankings_uses_all_backends():
    adapter = MagicMock()
    registry = _stub_registry()
    registry.translate.return_value = {}
    outcome = handle_routing(
        {"mode": "active", "server_rankings": []},
        registry, adapter, ["b1:8080", "b2:8080"],
    )
    assert outcome.applied is True
    adapter.set_upstream_weights.assert_called_once_with({"b1:8080": 1, "b2:8080": 1})


def test_handle_routing_merges_partial_ranking_with_known_pool():
    """G1: when RL publishes weights for only the eligible subset (PPO's
    is_eligible filter omits unhealthy/unknown backends), the omitted
    backends must still appear in upstream.conf — either as `down;` if
    excluded, or with a small floor weight. Without this, anomaly exclusion
    is silently bypassed on every RL publish.

    SOT §3.4 line 1756: "Policy safe_mode can short-circuit RL but never
    bypasses anomaly exclusion."
    """
    adapter = MagicMock()
    registry = _stub_registry()
    # RL only ranks 2 of 4 known backends (others were filtered as
    # unhealthy/unknown upstream).
    outcome = handle_routing(
        {"mode": "active", "server_rankings": [
            {"backend_id": "b1:8080", "score": 0.7},
            {"backend_id": "b2:8080", "score": 0.075},
        ]},
        registry, adapter,
        ["b1:8080", "b2:8080", "b3:8080", "b4:8080"],
    )
    assert outcome.applied is True
    # All 4 backends present in the applied weights.
    applied = adapter.set_upstream_weights.call_args[0][0]
    assert set(applied) == {"b1:8080", "b2:8080", "b3:8080", "b4:8080"}
    # RL-supplied weights override the floor for ranked backends.
    assert applied["b1:8080"] == 70
    assert applied["b2:8080"] == max(1, round(0.075 * 100))
    # Omitted backends get the floor — kept in the upstream block so
    # an anomaly-excluded backend stays `down;` (the adapter renders the
    # exclusion regardless of weight).
    assert applied["b3:8080"] == KNOWN_BACKEND_FLOOR_WEIGHT
    assert applied["b4:8080"] == KNOWN_BACKEND_FLOOR_WEIGHT


def test_handle_routing_confidence_below_threshold_rejected():
    """G3 / SOT §13 line 3128: rl_confidence_threshold — below this,
    sidecar ignores RL and uses classical. confidence = max(scores)."""
    adapter = MagicMock()
    registry = _stub_registry()
    outcome = handle_routing(
        {"mode": "active", "server_rankings": [
            {"backend_id": "b:8080", "score": 0.3},
        ]},
        registry, adapter, ["b:8080"],
        confidence_threshold=0.6,
    )
    assert outcome.applied is False
    assert outcome.rejected_below_threshold is True
    assert outcome.confidence == 0.3
    adapter.set_upstream_weights.assert_not_called()


def test_handle_routing_confidence_above_threshold_applies():
    adapter = MagicMock()
    registry = _stub_registry()
    outcome = handle_routing(
        {"mode": "active", "server_rankings": [
            {"backend_id": "b:8080", "score": 0.7},
        ]},
        registry, adapter, ["b:8080"],
        confidence_threshold=0.6,
    )
    assert outcome.applied is True
    assert outcome.rejected_below_threshold is False
    assert outcome.confidence == 0.7
    adapter.set_upstream_weights.assert_called_once()


def test_handle_routing_threshold_zero_disables_gate():
    """A threshold of 0 means the gate is off — every active envelope
    flows through. Default behaviour when no policy has been received."""
    adapter = MagicMock()
    registry = _stub_registry()
    outcome = handle_routing(
        {"mode": "active", "server_rankings": [
            {"backend_id": "b:8080", "score": 0.001},
        ]},
        registry, adapter, ["b:8080"],
        confidence_threshold=0.0,
    )
    assert outcome.applied is True


def test_handle_routing_shadow_reports_confidence():
    """Shadow envelopes still surface confidence so app.py can log it
    alongside the no-op decision."""
    outcome = handle_routing(
        {"mode": "shadow", "server_rankings": [
            {"backend_id": "b:8080", "score": 0.42},
        ]},
        _stub_registry(), MagicMock(), ["b:8080"],
    )
    assert outcome.applied is False
    assert outcome.mode == "shadow"
    assert outcome.confidence == 0.42


def test_handle_routing_active_case_insensitive():
    """Active mode comparison is case-insensitive (matches the
    effective_mode rule in rl-engine/runloop.py — M7 from v1.0.7)."""
    adapter = MagicMock()
    registry = _stub_registry()
    outcome = handle_routing(
        {"mode": "ACTIVE", "server_rankings": [
            {"backend_id": "b:8080", "score": 0.9},
        ]},
        registry, adapter, ["b:8080"],
    )
    assert outcome.applied is True


def test_handle_routing_adapter_error_captured():
    adapter = MagicMock()
    adapter.set_upstream_weights.side_effect = RuntimeError("docker fail")
    registry = _stub_registry()
    outcome = handle_routing(
        {"mode": "active", "server_rankings": [{"backend_id": "b:8080", "score": 0.9}]},
        registry, adapter, ["b:8080"],
    )
    assert outcome.applied is False
    assert "docker fail" in outcome.error


# ── handle_anomaly ────────────────────────────────────────────────────────────

def test_handle_anomaly_unhealthy_excludes():
    adapter = MagicMock()
    registry = MagicMock(spec=BackendRegistry)
    registry.translate_one.return_value = "backend-1:8080"
    outcome = handle_anomaly(
        {"backend_id": "172.18.0.5:8080", "status": "unhealthy", "score": 0.9},
        registry, adapter,
    )
    assert outcome.applied is True
    assert outcome.action == "exclude"
    adapter.exclude_backend.assert_called_once_with("backend-1:8080")


def test_handle_anomaly_healthy_includes():
    adapter = MagicMock()
    registry = MagicMock(spec=BackendRegistry)
    registry.translate_one.return_value = "backend-2:8080"
    outcome = handle_anomaly(
        {"backend_id": "172.18.0.6:8080", "status": "healthy", "score": 0.1},
        registry, adapter,
    )
    assert outcome.applied is True
    assert outcome.action == "include"
    adapter.include_backend.assert_called_once_with("backend-2:8080")


def test_handle_anomaly_degraded_includes():
    adapter = MagicMock()
    registry = MagicMock(spec=BackendRegistry)
    registry.translate_one.return_value = "backend-3:8080"
    outcome = handle_anomaly(
        {"backend_id": "172.18.0.7:8080", "status": "degraded", "score": 0.5},
        registry, adapter,
    )
    assert outcome.action == "include"


def test_handle_anomaly_error_captured():
    adapter = MagicMock()
    adapter.exclude_backend.side_effect = RuntimeError("boom")
    registry = MagicMock(spec=BackendRegistry)
    registry.translate_one.return_value = "b:8080"
    outcome = handle_anomaly(
        {"backend_id": "1.2.3.4:8080", "status": "unhealthy", "score": 0.99},
        registry, adapter,
    )
    assert outcome.applied is False
    assert "boom" in outcome.error


# ── handle_policy ─────────────────────────────────────────────────────────────

def test_handle_policy_safe_mode_reverts_weights():
    adapter = MagicMock()
    backends = ["b1:8080", "b2:8080", "b3:8080"]
    outcome = handle_policy({"safe_mode": True}, adapter, backends)
    assert outcome.applied is True
    assert outcome.safe_mode is True
    adapter.set_upstream_weights.assert_called_once_with(
        {"b1:8080": 1, "b2:8080": 1, "b3:8080": 1}
    )


def test_handle_policy_no_safe_mode_is_noop():
    adapter = MagicMock()
    outcome = handle_policy({"safe_mode": False}, adapter, ["b:8080"])
    assert outcome.applied is False
    adapter.set_upstream_weights.assert_not_called()


def test_handle_policy_missing_safe_mode_is_noop():
    adapter = MagicMock()
    outcome = handle_policy({}, adapter, ["b:8080"])
    assert outcome.applied is False


def test_handle_policy_error_captured():
    adapter = MagicMock()
    adapter.set_upstream_weights.side_effect = RuntimeError("oops")
    outcome = handle_policy({"safe_mode": True}, adapter, ["b:8080"])
    assert outcome.applied is False
    assert "oops" in outcome.error


def test_handle_policy_tracks_rl_confidence_threshold():
    """G3 / SOT §13 line 3128: PolicyState mutated in place so subsequent
    handle_routing calls see the new threshold."""
    state = PolicyState()
    adapter = MagicMock()
    outcome = handle_policy(
        {"safe_mode": False, "rl_confidence_threshold": 0.75},
        adapter, ["b:8080"],
        policy_state=state,
    )
    assert outcome.applied is False
    assert outcome.rl_confidence_threshold == 0.75
    assert state.rl_confidence_threshold == 0.75
    assert state.safe_mode is False


def test_handle_policy_safe_mode_also_updates_threshold():
    state = PolicyState(rl_confidence_threshold=0.0)
    adapter = MagicMock()
    outcome = handle_policy(
        {"safe_mode": True, "rl_confidence_threshold": 0.6},
        adapter, ["b:8080"],
        policy_state=state,
    )
    assert outcome.applied is True
    assert outcome.safe_mode is True
    assert state.safe_mode is True
    assert state.rl_confidence_threshold == 0.6


def test_handle_policy_partial_publish_preserves_previous_threshold():
    """A policy publish that omits rl_confidence_threshold must not
    wipe the previous value to 0 — fall back to PolicyState."""
    state = PolicyState(rl_confidence_threshold=0.6)
    adapter = MagicMock()
    outcome = handle_policy(
        {"safe_mode": True},     # threshold omitted
        adapter, ["b:8080"],
        policy_state=state,
    )
    assert outcome.applied is True
    assert state.rl_confidence_threshold == 0.6
    assert outcome.rl_confidence_threshold == 0.6


def test_handle_policy_malformed_threshold_falls_back():
    state = PolicyState(rl_confidence_threshold=0.5)
    adapter = MagicMock()
    outcome = handle_policy(
        {"safe_mode": False, "rl_confidence_threshold": "not-a-number"},
        adapter, ["b:8080"],
        policy_state=state,
    )
    assert state.rl_confidence_threshold == 0.5
    assert outcome.rl_confidence_threshold == 0.5
