"""
tests/unit/rl-engine/test_runloop.py
─────────────────────────────────────
Pure-Python unit tests for services/rl-engine/runloop.py.

No Docker, no Redis, no DB — runs in the unit-tests CI job.

Coverage:
  1. classify_health — boundary cases for unhealthy / degraded / healthy.
  2. bootstrap_policy — happy path, fallback when requested policy fails,
                        baseline failure propagates, kwarg compatibility
                        (TypeError-on-construct → fall back to no-kwargs).
  3. policy_from_payload — partial payloads, malformed types, safe_mode
                           coercion.
  4. build_state_from_rows — single row, multiple rows, null values,
                             malformed row skip.
  5. effective_mode — every cell of the truth table
                      (safe_mode × RL_MODE × policy.mode).
  6. should_publish — empty state gates publish; non-empty allows.
  7. action_to_event_payload — dict shape matches RoutingRecommendation.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add services/rl-engine/ to sys.path so we can import runloop + policy_base
# + the plugin folders from this test file.
_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "rl-engine"
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from policy_base import BackendState, Ranking, RoutingAction  # noqa: E402
from runloop import (                                          # noqa: E402
    ACTIVE,
    DEFAULT_RL_CONFIDENCE_THRESHOLD,
    DEFAULT_RL_EXPLORATION_RATE,
    DEGRADED_LATENCY_MS,
    EnginePolicy,
    SHADOW,
    UNHEALTHY_ERROR_RATE,
    action_to_event_payload,
    bootstrap_policy,
    build_state_from_rows,
    classify_health,
    effective_mode,
    policy_from_payload,
    serialize_engine_state,
    should_publish,
)


# ── classify_health ──────────────────────────────────────────────────────────

def test_health_healthy_when_both_low():
    assert classify_health(50.0, 0.0) == "healthy"


def test_health_unhealthy_when_error_rate_exceeds_threshold():
    assert classify_health(50.0, UNHEALTHY_ERROR_RATE + 0.01) == "unhealthy"


def test_health_degraded_when_latency_exceeds_threshold():
    assert classify_health(DEGRADED_LATENCY_MS + 1.0, 0.0) == "degraded"


def test_health_unhealthy_takes_priority_over_degraded():
    """Both high → unhealthy wins because it's the worse signal."""
    assert classify_health(500.0, 0.5) == "unhealthy"


def test_health_boundary_values_are_healthy():
    """Threshold values themselves should be considered healthy (strict >)."""
    assert classify_health(DEGRADED_LATENCY_MS, UNHEALTHY_ERROR_RATE) == "healthy"


# ── bootstrap_policy ─────────────────────────────────────────────────────────

def test_bootstrap_random_shadow_succeeds():
    boot = bootstrap_policy("random_shadow", EnginePolicy())
    assert boot.ready is True
    assert boot.name == "random_shadow"
    assert boot.requested == "random_shadow"
    assert boot.error is None


def test_bootstrap_unknown_policy_falls_back_to_baseline():
    boot = bootstrap_policy("definitely-not-a-real-policy", EnginePolicy())
    assert boot.ready is False
    assert boot.name == "random_shadow"
    assert boot.requested == "definitely-not-a-real-policy"
    assert boot.error is not None
    # Policy still produces an action — fallback is functional, not a no-op.
    state = [BackendState("b1", 10.0, 5, "healthy")]
    action = boot.policy.act(state)
    assert isinstance(action, RoutingAction)
    assert action.mode == "shadow"
    assert len(action.rankings) == 1


def test_bootstrap_kwarg_mismatch_falls_through_to_no_kwargs():
    """random_shadow accepts only `seed`, not confidence_threshold/
    exploration_rate. The bootstrap helper must retry without kwargs
    rather than failing — otherwise valid policies couldn't load."""
    policy = EnginePolicy(rl_exploration_rate=0.2, rl_confidence_threshold=0.7)
    boot = bootstrap_policy("random_shadow", policy)
    assert boot.ready is True
    assert boot.name == "random_shadow"


# ── policy_from_payload ──────────────────────────────────────────────────────

def test_policy_from_full_payload():
    new = policy_from_payload({
        "rl_confidence_threshold": 0.8,
        "rl_exploration_rate":     0.15,
        "safe_mode":               True,
        "policy_version":          9,
    }, fallback=EnginePolicy())
    assert new.rl_confidence_threshold == 0.8
    assert new.rl_exploration_rate == 0.15
    assert new.safe_mode is True
    assert new.policy_version == 9


def test_policy_missing_fields_use_fallback():
    fallback = EnginePolicy(rl_confidence_threshold=0.75,
                            rl_exploration_rate=0.1,
                            policy_version=3, safe_mode=True)
    new = policy_from_payload({}, fallback=fallback)
    assert new.rl_confidence_threshold == 0.75
    assert new.rl_exploration_rate == 0.1
    assert new.policy_version == 3
    assert new.safe_mode is True


def test_policy_malformed_floats_fall_back():
    fallback = EnginePolicy(rl_confidence_threshold=0.6,
                            rl_exploration_rate=0.05)
    new = policy_from_payload({
        "rl_confidence_threshold": "not-a-number",
        "rl_exploration_rate":     "nope",
    }, fallback=fallback)
    assert new.rl_confidence_threshold == 0.6
    assert new.rl_exploration_rate == 0.05


# ── build_state_from_rows ────────────────────────────────────────────────────

def test_state_empty_rows():
    assert build_state_from_rows([]) == []


def test_state_single_row():
    rows = [("backend_1", 150.0, 200, 0.01)]
    state = build_state_from_rows(rows)
    assert len(state) == 1
    s = state[0]
    assert s.backend_id == "backend_1"
    assert s.latency_ms == 150.0
    assert s.queue_depth == 200
    assert s.health == "healthy"     # below both thresholds


def test_state_multiple_rows_classify_health():
    rows = [
        ("b_healthy",   100.0, 50,   0.0),
        ("b_degraded",  500.0, 80,   0.01),
        ("b_unhealthy", 100.0, 30,   0.2),
    ]
    state = build_state_from_rows(rows)
    by_id = {s.backend_id: s for s in state}
    assert by_id["b_healthy"].health == "healthy"
    assert by_id["b_degraded"].health == "degraded"
    assert by_id["b_unhealthy"].health == "unhealthy"


def test_state_handles_nulls_from_db():
    """All-NULL telemetry → health="unknown", not "healthy". Silence is not
    a positive signal; classifying it as healthy would mask a backend whose
    metrics stopped flowing (lb-otel-shipper bug, partition, etc.)."""
    rows = [("b1", None, None, None)]
    state = build_state_from_rows(rows)
    assert state[0].latency_ms == 0.0
    assert state[0].queue_depth == 0
    assert state[0].health == "unknown"


def test_state_partial_null_still_classifies():
    """At least one signal present (latency XOR error_rate) → fall back to
    classify_health rather than UNKNOWN. UNKNOWN is for total silence."""
    # Only latency present, error_rate NULL → classify on latency
    rows = [("b1", 50.0, 10, None)]
    state = build_state_from_rows(rows)
    assert state[0].health == "healthy"

    # Only error_rate present, latency NULL → classify on error_rate
    rows = [("b2", None, 0, 0.5)]
    state = build_state_from_rows(rows)
    assert state[0].health == "unhealthy"


def test_state_skips_malformed_row():
    rows = [
        ("b1", 10.0, 5, 0.0),
        ("bad-row",),                # too few columns
        ("b2", 20.0, 10, 0.0),
    ]
    state = build_state_from_rows(rows)
    assert [s.backend_id for s in state] == ["b1", "b2"]


# ── effective_mode (truth table) ─────────────────────────────────────────────

def _ep(safe_mode=False):
    return EnginePolicy(safe_mode=safe_mode)


def test_mode_safe_mode_forces_shadow():
    """safe_mode=true → always shadow, regardless of other inputs."""
    assert effective_mode(ACTIVE, "active", _ep(safe_mode=True)) == SHADOW
    assert effective_mode(SHADOW, "active", _ep(safe_mode=True)) == SHADOW
    assert effective_mode(ACTIVE, "shadow", _ep(safe_mode=True)) == SHADOW


def test_mode_env_shadow_forces_shadow():
    """RL_MODE != "active" pins shadow regardless of policy intent."""
    assert effective_mode(ACTIVE, "shadow", _ep()) == SHADOW
    assert effective_mode(ACTIVE, "off",    _ep()) == SHADOW
    assert effective_mode(ACTIVE, "",       _ep()) == SHADOW


def test_mode_policy_must_agree_for_active():
    """Even with RL_MODE=active and safe_mode=false, the policy itself
    must return mode='active' to publish active; otherwise shadow."""
    assert effective_mode(ACTIVE, "active", _ep()) == ACTIVE
    assert effective_mode(SHADOW, "active", _ep()) == SHADOW


def test_mode_rl_mode_env_is_case_insensitive():
    assert effective_mode(ACTIVE, "ACTIVE", _ep()) == ACTIVE
    assert effective_mode(ACTIVE, "Active", _ep()) == ACTIVE


def test_mode_action_mode_is_case_insensitive():
    """M7: action.mode comparison must match the env-var rule. A policy
    emitting "Active" or "ACTIVE" should still produce effective_mode=active
    when the other gates agree."""
    assert effective_mode("Active", "active", _ep()) == ACTIVE
    assert effective_mode("ACTIVE", "active", _ep()) == ACTIVE
    assert effective_mode("active", "active", _ep()) == ACTIVE
    # Empty / None still resolves to shadow.
    assert effective_mode("", "active", _ep()) == SHADOW


# ── should_publish ───────────────────────────────────────────────────────────

def test_should_publish_blocks_on_empty_state():
    assert should_publish([]) is False


def test_should_publish_allows_non_empty():
    assert should_publish([BackendState("b1", 10.0, 5, "healthy")]) is True


# ── action_to_event_payload ──────────────────────────────────────────────────

def test_payload_shape():
    action = RoutingAction(mode="shadow", rankings=[
        Ranking(backend_id="b1", score=0.3),
        Ranking(backend_id="b2", score=0.7),
    ])
    payload = action_to_event_payload(action, mode="shadow", policy_version=7)
    assert payload == {
        "mode": "shadow",
        "server_rankings": [
            {"backend_id": "b1", "score": 0.3},
            {"backend_id": "b2", "score": 0.7},
        ],
        "policy_version": 7,
    }


def test_payload_uses_effective_mode_not_action_mode():
    """Even if the action says 'active', the published mode must reflect
    what the run loop decided (could be 'shadow' due to safe_mode etc.)."""
    action = RoutingAction(mode="active", rankings=[Ranking("b1", 0.5)])
    payload = action_to_event_payload(action, mode="shadow", policy_version=0)
    assert payload["mode"] == "shadow"


# ── EnginePolicy basics ──────────────────────────────────────────────────────

def test_engine_policy_defaults():
    p = EnginePolicy()
    assert p.rl_confidence_threshold == DEFAULT_RL_CONFIDENCE_THRESHOLD
    assert p.rl_exploration_rate == DEFAULT_RL_EXPLORATION_RATE
    assert p.safe_mode is False
    assert p.policy_version == 0
    assert p.operating_mode == "shadow"


def test_policy_kwargs_includes_constructor_params():
    p = EnginePolicy(rl_confidence_threshold=0.9, rl_exploration_rate=0.2)
    assert p.policy_kwargs() == {
        "confidence_threshold": 0.9,
        "exploration_rate":     0.2,
        "operating_mode":       "shadow",
    }


def test_policy_from_payload_classical_maps_to_shadow():
    """policy.yaml: operating_mode=classical → internal shadow (Amendment B)."""
    new = policy_from_payload({"operating_mode": "classical"}, fallback=EnginePolicy())
    assert new.operating_mode == "shadow"


def test_policy_from_payload_operating_mode_passthrough():
    for mode in ("shadow", "hybrid"):
        new = policy_from_payload({"operating_mode": mode}, fallback=EnginePolicy())
        assert new.operating_mode == mode


def test_policy_from_payload_learning_maps_to_hybrid():
    """Deprecated "learning" value maps to "hybrid" for backwards-compat
    with older policy.yaml files. See M10 in the RL review."""
    new = policy_from_payload({"operating_mode": "learning"}, fallback=EnginePolicy())
    assert new.operating_mode == "hybrid"


def test_policy_from_payload_unknown_mode_falls_back():
    fallback = EnginePolicy(operating_mode="hybrid")
    new = policy_from_payload({"operating_mode": "nonsense"}, fallback=fallback)
    assert new.operating_mode == "hybrid"


def test_policy_from_payload_missing_mode_uses_fallback():
    fallback = EnginePolicy(operating_mode="hybrid")
    new = policy_from_payload({}, fallback=fallback)
    assert new.operating_mode == "hybrid"


# ── serialize_engine_state (Live Engines #121) ───────────────────────────────

def _state_kwargs(**overrides):
    base = dict(
        service="rl-engine",
        channel="smartload.routing",
        runloop_enabled=True,
        policy_name="random_shadow",
        policy_requested="random_shadow",
        policy_ready=True,
        policy_error=None,
        engine_policy=EnginePolicy(rl_exploration_rate=0.1, policy_version=4),
        rl_mode_env="shadow",
        ticks_total=15,
        publishes_total=15,
        last_tick_at="2026-05-24T19:30:00+00:00",
        last_publish_at="2026-05-24T19:30:00+00:00",
        last_tick_monotonic=None,
        last_output=None,
    )
    base.update(overrides)
    return base


def test_state_shape_has_every_top_level_key():
    body = serialize_engine_state(**_state_kwargs())
    # rl-engine adds rl_mode_env at the top level — unique to this service.
    assert set(body) == {
        "service", "channel", "runloop_enabled", "rl_mode_env",
        "engine", "policy_snapshot", "stats", "last_output",
    }


def test_state_engine_kind_is_policy_for_rl():
    body = serialize_engine_state(**_state_kwargs())
    assert body["engine"]["kind"] == "policy"
    assert body["engine"]["loaded"] == "random_shadow"
    assert body["engine"]["ready"] is True


def test_state_rl_mode_env_reflects_operator_pin():
    body = serialize_engine_state(**_state_kwargs(rl_mode_env="active"))
    assert body["rl_mode_env"] == "active"


def test_state_carries_routing_action_payload():
    out = {
        "mode": "shadow",
        "server_rankings": [
            {"backend_id": "b1", "score": 0.7},
            {"backend_id": "b2", "score": 0.3},
        ],
        "policy_version": 4,
    }
    body = serialize_engine_state(**_state_kwargs(last_output=out))
    assert body["last_output"] == out
    assert body["last_output"]["mode"] == "shadow"


def test_state_reports_policy_load_failure():
    body = serialize_engine_state(**_state_kwargs(
        policy_name="random_shadow",
        policy_requested="ppo",
        policy_ready=False,
        policy_error="policy.zip missing",
    ))
    assert body["engine"]["requested"] == "ppo"
    assert body["engine"]["loaded"] == "random_shadow"
    assert body["engine"]["ready"] is False
    assert body["engine"]["error"] == "policy.zip missing"
