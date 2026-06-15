"""
tests/unit/lb-sidecar/test_runloop.py
──────────────────────────────────────
Pure-Python unit tests for services/lb-sidecar/runloop.py.

No Docker, no Redis, no filesystem — runs in the unit-tests CI job.

Coverage:
  1. scores_to_weights — score→weight mapping, zero-score floor, empty list.
  1b. clamp_weight_skew — degenerate vector bounded, discriminating vector
                          untouched, disabled when fraction<=0.
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
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_SERVICE = Path(__file__).resolve().parents[3] / "services" / "lb-sidecar"
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from runloop import (  # noqa: E402
    BackendRegistry,
    AnomalyOutcome,
    PolicyOutcome,
    PolicyState,
    RoutingOutcome,
    ScaleOutcome,
    handle_anomaly,
    handle_policy,
    handle_routing,
    handle_scale,
    clamp_weight_skew,
    normalize_backend_key,
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


# ── clamp_weight_skew ─────────────────────────────────────────────────────────

def test_clamp_weight_skew_bounds_degenerate_vector():
    # The pathological case: one backend ≈1.0, the rest ≈0 -> ~100:1 skew that
    # pins all traffic on b1 and overflows its queue under a uniform surge.
    # With the 0.75 floor the starved backends are raised to round(100*0.75)=75.
    weights = {"b1:8080": 100, "b2:8080": 1, "b3:8080": 1, "b4:8080": 1}
    assert clamp_weight_skew(weights) == {
        "b1:8080": 100, "b2:8080": 75, "b3:8080": 75, "b4:8080": 75,
    }


def test_clamp_weight_skew_noop_when_already_balanced():
    # A ranking already inside the ~1.33:1 band passes through unchanged — the
    # rail only bites spreads wider than the suppressor's outlier margin.
    weights = {"b1:8080": 80, "b2:8080": 70, "b3:8080": 62}
    assert clamp_weight_skew(weights) == weights


def test_clamp_weight_skew_never_lowers_top():
    weights = {"b1:8080": 100, "b2:8080": 90}
    out = clamp_weight_skew(weights)
    assert out["b1:8080"] == 100  # top is never reduced
    assert out["b2:8080"] == 90   # already within band


def test_clamp_weight_skew_disabled_when_fraction_zero():
    weights = {"b1:8080": 100, "b2:8080": 1}
    assert clamp_weight_skew(weights, min_fraction=0.0) == weights


def test_clamp_weight_skew_empty():
    assert clamp_weight_skew({}) == {}


def test_clamp_weight_skew_floor_at_least_one():
    # Tiny top weight -> floor rounds toward 1, never 0 (NGINX rejects weight=0).
    weights = {"b1:8080": 1, "b2:8080": 1}
    out = clamp_weight_skew(weights)
    assert all(w >= 1 for w in out.values())


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
    # The skew rail (~1.33:1) is applied to the MERGED pool, so the top ranked
    # backend keeps its raw weight (70) and every other backend — the low-ranked
    # b2 (raw 8) AND the omitted/floored b3, b4 — is lifted to floor =
    # round(70*0.75) = 52. Omitted HEALTHY backends thus carry a fair share
    # instead of starving at weight 1 while the ranked few overflow under load.
    assert applied["b1:8080"] == 70
    assert applied["b2:8080"] == 52
    assert applied["b3:8080"] == 52
    assert applied["b4:8080"] == 52


def test_handle_routing_clamp_disabled_via_fraction_zero():
    # clamp_min_fraction=0 disables the skew rail (ablation isolation): the low
    # ranked b2 keeps its raw weight (8) and omitted b3/b4 stay at the floor (1),
    # reproducing the pre-clamp behaviour for the ablation's "-clamp" config.
    adapter = MagicMock()
    registry = _stub_registry()
    handle_routing(
        {"mode": "active", "server_rankings": [
            {"backend_id": "b1:8080", "score": 0.7},
            {"backend_id": "b2:8080", "score": 0.08},
        ]},
        registry, adapter,
        ["b1:8080", "b2:8080", "b3:8080", "b4:8080"],
        clamp_min_fraction=0.0,
    )
    applied = adapter.set_upstream_weights.call_args[0][0]
    assert applied["b1:8080"] == 70
    assert applied["b2:8080"] == 8
    assert applied["b3:8080"] == 1
    assert applied["b4:8080"] == 1


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

def _adapter_with_pool(weights, excluded=()):
    """A mock adapter whose current_state() reports a real pool snapshot,
    so the quorum guard in handle_anomaly can reason about it."""
    adapter = MagicMock()
    adapter.current_state.return_value = SimpleNamespace(
        upstream_weights=dict(weights),
        excluded_backends=set(excluded),
    )
    return adapter


def test_handle_anomaly_unhealthy_excludes():
    # Two-backend pool: excluding backend-1 leaves backend-2 serving, so
    # the quorum guard allows it.
    adapter = _adapter_with_pool({"backend-1:8080": 1, "backend-2:8080": 1})
    registry = MagicMock(spec=BackendRegistry)
    registry.translate_one.return_value = "backend-1:8080"
    outcome = handle_anomaly(
        {"backend_id": "172.18.0.5:8080", "status": "unhealthy", "score": 0.9},
        registry, adapter,
    )
    assert outcome.applied is True
    assert outcome.action == "exclude"
    adapter.exclude_backend.assert_called_once_with("backend-1:8080")


def test_handle_anomaly_refuses_to_exclude_last_active_backend():
    """Quorum guard: an unhealthy verdict on the only serving backend is
    refused — excluding it would empty the upstream and 502 the pool (the
    failure mode behind the v1.0.7an isolation_forest revert)."""
    adapter = _adapter_with_pool({"backend-1:8080": 1})
    registry = MagicMock(spec=BackendRegistry)
    registry.translate_one.return_value = "backend-1:8080"
    outcome = handle_anomaly(
        {"backend_id": "172.18.0.5:8080", "status": "unhealthy", "score": 0.99},
        registry, adapter,
    )
    assert outcome.applied is False
    assert outcome.action == "noop"
    assert "quorum guard" in (outcome.error or "")
    adapter.exclude_backend.assert_not_called()


def test_handle_anomaly_refuses_when_only_peer_already_excluded():
    """Two-backend pool with one already excluded: the survivor going
    unhealthy must not be excluded too."""
    adapter = _adapter_with_pool(
        {"backend-1:8080": 1, "backend-2:8080": 1},
        excluded=["backend-2:8080"],
    )
    registry = MagicMock(spec=BackendRegistry)
    registry.translate_one.return_value = "backend-1:8080"
    outcome = handle_anomaly(
        {"backend_id": "10.0.0.1:8080", "status": "unhealthy", "score": 0.9},
        registry, adapter,
    )
    assert outcome.applied is False
    assert outcome.action == "noop"
    adapter.exclude_backend.assert_not_called()


def test_handle_anomaly_excludes_when_a_healthy_peer_remains():
    """With a healthy peer still in the pool, exclusion proceeds normally."""
    adapter = _adapter_with_pool({"backend-1:8080": 1, "backend-2:8080": 1})
    registry = MagicMock(spec=BackendRegistry)
    registry.translate_one.return_value = "backend-1:8080"
    outcome = handle_anomaly(
        {"backend_id": "10.0.0.1:8080", "status": "unhealthy", "score": 0.9},
        registry, adapter,
    )
    assert outcome.applied is True
    assert outcome.action == "exclude"
    adapter.exclude_backend.assert_called_once_with("backend-1:8080")


def test_handle_anomaly_guard_degrades_safely_without_state():
    """If current_state() is unavailable/malformed the guard must not block
    dispatch — exclusion proceeds exactly as before the guard existed."""
    adapter = MagicMock()
    adapter.current_state.side_effect = RuntimeError("no state")
    registry = MagicMock(spec=BackendRegistry)
    registry.translate_one.return_value = "backend-1:8080"
    outcome = handle_anomaly(
        {"backend_id": "10.0.0.1:8080", "status": "unhealthy", "score": 0.9},
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


def test_handle_anomaly_ignores_backend_pool_sentinel():
    """Membership guard: a verdict for the NGINX upstream block name
    `backend_pool` (the all-down 502 sentinel the detector can mistakenly
    score) is dropped before it reaches the exclusion path. Without this the
    phantom exclusion empties the pool — a self-sustaining outage
    (audit/_findings/anomaly-pool-collapse-rootcause)."""
    adapter = _adapter_with_pool({"backend-1:8080": 1, "backend-2:8080": 1})
    registry = MagicMock(spec=BackendRegistry)
    registry.translate_one.return_value = "backend_pool"   # not a real backend
    outcome = handle_anomaly(
        {"backend_id": "backend_pool", "status": "unhealthy", "score": 1.0},
        registry, adapter,
        live_backends=["backend-1:8080", "backend-2:8080"],
    )
    assert outcome.applied is False
    assert outcome.action == "noop"
    assert "unknown backend" in (outcome.error or "")
    adapter.exclude_backend.assert_not_called()


def test_handle_anomaly_acts_on_real_backend_in_live_pool():
    """A verdict that names a backend present in the live pool is processed
    normally even with the membership guard active."""
    adapter = _adapter_with_pool({"backend-1:8080": 1, "backend-2:8080": 1})
    registry = MagicMock(spec=BackendRegistry)
    registry.translate_one.return_value = "backend-1:8080"
    outcome = handle_anomaly(
        {"backend_id": "10.0.0.1:8080", "status": "unhealthy", "score": 0.9},
        registry, adapter,
        live_backends=["backend-1:8080", "backend-2:8080"],
    )
    assert outcome.applied is True
    assert outcome.action == "exclude"
    adapter.exclude_backend.assert_called_once_with("backend-1:8080")


def test_handle_anomaly_membership_guard_skipped_when_no_live_pool():
    """live_backends=None (older callers / tests) skips the membership check
    and preserves the prior behaviour."""
    adapter = _adapter_with_pool({"backend-1:8080": 1, "backend-2:8080": 1})
    registry = MagicMock(spec=BackendRegistry)
    registry.translate_one.return_value = "backend-1:8080"
    outcome = handle_anomaly(
        {"backend_id": "10.0.0.1:8080", "status": "unhealthy", "score": 0.9},
        registry, adapter,
    )
    assert outcome.applied is True
    assert outcome.action == "exclude"


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


# ── handle_scale (#164) ───────────────────────────────────────────────────────

def test_handle_scale_out_writes_equal_weights_across_live_pool():
    """A scale_out event grows the pool from 3 to 4 backends; the handler
    writes an equal-weight upstream map covering the new live pool so the
    new backend enters NGINX's upstream block."""
    adapter = MagicMock()
    outcome = handle_scale(
        {"action": "scale_out", "instance_count": 4, "mechanism": "provision"},
        adapter,
        ["smartload-test-backend-1:8080",
         "smartload-test-backend-2:8080",
         "smartload-test-backend-3:8080",
         "smartload-test-backend-4:8080"],
    )
    assert outcome.applied is True
    assert outcome.backend_count == 4
    assert outcome.action == "scale_out"
    assert outcome.mechanism == "provision"
    applied = adapter.set_upstream_weights.call_args[0][0]
    assert set(applied) == {
        "smartload-test-backend-1:8080",
        "smartload-test-backend-2:8080",
        "smartload-test-backend-3:8080",
        "smartload-test-backend-4:8080",
    }
    assert all(w == 1 for w in applied.values())


def test_handle_scale_in_shrinks_upstream_map():
    """A scale_in event removes a backend from the live pool; the handler
    writes the smaller map so the stopped container leaves upstream.conf."""
    adapter = MagicMock()
    outcome = handle_scale(
        {"action": "scale_in", "instance_count": 2, "mechanism": "stop"},
        adapter,
        ["smartload-test-backend-1:8080",
         "smartload-test-backend-2:8080"],
    )
    assert outcome.applied is True
    assert outcome.backend_count == 2
    applied = adapter.set_upstream_weights.call_args[0][0]
    assert set(applied) == {
        "smartload-test-backend-1:8080",
        "smartload-test-backend-2:8080",
    }


def test_handle_scale_passes_mechanism_through_outcome():
    """The outcome carries the mechanism field unchanged so app.py can
    log the lifecycle path (start | provision | stop | decommission)."""
    adapter = MagicMock()
    for mech in ("start", "provision", "stop", "decommission"):
        outcome = handle_scale(
            {"action": "scale_out", "mechanism": mech},
            adapter,
            ["b:8080"],
        )
        assert outcome.mechanism == mech


def test_handle_scale_empty_live_backends_refuses_to_write():
    """Refusing to write an empty upstream block is the safety pin: if the
    docker query came back empty (daemon unreachable, transient hiccup),
    we DON'T want to leave NGINX with no upstreams."""
    adapter = MagicMock()
    outcome = handle_scale(
        {"action": "scale_in"}, adapter, [],
    )
    assert outcome.applied is False
    assert outcome.error is not None
    assert "no live backends" in outcome.error
    adapter.set_upstream_weights.assert_not_called()


def test_handle_scale_adapter_idempotent_no_op_still_applied():
    """The adapter short-circuits when the new weights match the current
    upstream map. handle_scale still reports applied=True because the
    instruction was successfully delivered — whether the adapter wrote
    or no-op'd is an implementation detail beyond this layer."""
    adapter = MagicMock()
    # Even when set_upstream_weights raises nothing, the outcome is applied.
    outcome = handle_scale(
        {"action": "scale_out"},
        adapter, ["b:8080"],
    )
    assert outcome.applied is True
    adapter.set_upstream_weights.assert_called_once_with({"b:8080": 1})


def test_handle_scale_adapter_error_captured():
    adapter = MagicMock()
    adapter.set_upstream_weights.side_effect = RuntimeError("docker reload failed")
    outcome = handle_scale(
        {"action": "scale_out"}, adapter, ["b:8080"],
    )
    assert outcome.applied is False
    assert outcome.error is not None
    assert "docker reload failed" in outcome.error


def test_handle_scale_normalises_action_lowercase():
    """ScalingEvent.action is canonically lowercase but the handler is
    defensive against wire variants. The outcome.action mirrors what was
    received, normalised to lowercase."""
    adapter = MagicMock()
    outcome = handle_scale(
        {"action": "SCALE_OUT"}, adapter, ["b:8080"],
    )
    assert outcome.action == "scale_out"


def test_handle_scale_missing_mechanism_yields_none():
    """Older publishers without #155's mechanism field still parse
    cleanly — the outcome's mechanism is None rather than KeyError."""
    adapter = MagicMock()
    outcome = handle_scale(
        {"action": "scale_out"}, adapter, ["b:8080"],
    )
    assert outcome.mechanism is None


def test_handle_scale_reconciles_stale_exclusions(tmp_path):
    """N3: a scale event prunes exclusions for backends no longer in the live
    pool so a stale `down;` cannot persist against a removed member."""
    import socket as _socket
    from unittest.mock import patch as _patch
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "services"))
    from shared.lb_adapters.nginx import NginxAdapter  # noqa: E402

    docker = MagicMock()
    container = MagicMock()
    container.exec_run.return_value = (0, b"")
    docker.containers.get.return_value = container
    conf = tmp_path / "upstream.conf"

    with _patch.object(_socket, "gethostbyname", return_value="127.0.0.1"):
        adapter = NginxAdapter(
            conf_path=conf,
            nginx_container="lb",
            docker_client=docker,
            all_backends=["b1:8080", "b2:8080"],
            dns_preflight=False,
        )
        adapter.set_upstream_weights({"b1:8080": 1, "b2:8080": 1})
        adapter.exclude_backend("b2:8080")
        assert "b2:8080" in adapter.current_state().excluded_backends

        # b2 leaves the live pool (scale_in). The handler must drop its stale
        # exclusion so it doesn't gate the quorum guard against a ghost member.
        outcome = handle_scale(
            {"action": "scale_in", "mechanism": "stop"},
            adapter,
            ["b1:8080"],
        )

    assert outcome.applied is True
    assert "b2:8080" not in adapter.current_state().excluded_backends
    assert "b1:8080" not in adapter.current_state().excluded_backends


# ── normalize_backend_key (L4) ────────────────────────────────────────────────

def test_normalize_backend_key_adds_default_port():
    """L4: a bare hostname is normalised to host:8080 so the exclusion key
    matches the upstream weight keys (which always carry :port)."""
    assert normalize_backend_key("smartload-test-backend-1") == \
        "smartload-test-backend-1:8080"


def test_normalize_backend_key_preserves_existing_port():
    assert normalize_backend_key("backend-1:8080") == "backend-1:8080"
    assert normalize_backend_key("172.18.0.5:8080") == "172.18.0.5:8080"


def test_normalize_backend_key_empty_passthrough():
    assert normalize_backend_key("") == ""


def test_handle_anomaly_normalises_bare_name_to_port():
    """L4: when the registry returns a bare hostname (id not in its IP map),
    the exclusion is keyed host:8080 so it matches the weight keys and the
    quorum guard / renderer can see it."""
    adapter = _adapter_with_pool(
        {"backend-1:8080": 1, "backend-2:8080": 1},
    )
    registry = MagicMock(spec=BackendRegistry)
    # Registry passes a bare name through (the id wasn't an IP it could map).
    registry.translate_one.return_value = "backend-1"
    outcome = handle_anomaly(
        {"backend_id": "backend-1", "status": "unhealthy", "score": 0.9},
        registry, adapter,
    )
    assert outcome.applied is True
    assert outcome.action == "exclude"
    # Excluded under the :8080 key, not the bare name.
    adapter.exclude_backend.assert_called_once_with("backend-1:8080")
