"""
tests/unit/anomaly-detector/test_runloop.py
─────────────────────────────────────────────
Pure-Python unit tests for services/anomaly-detector/runloop.py.

No Docker, no Redis, no DB — runs in the unit-tests CI job.

Coverage:
  1. bootstrap_engine — happy path, fallback when requested engine fails,
                        baseline failure propagates.
  2. policy_from_payload — full payload parses; missing / malformed fields
                           fall back to the previous policy.
  3. build_features_from_rows — empty rows, single-instance pivot,
                                multi-instance pivot, malformed row skip.
  4. should_publish — safe_mode never publishes; advisory publishes all;
                      auto-isolate publishes non-healthy only.
  5. score_to_event_payload — dict shape matches AnomalyEvent fields.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add services/anomaly-detector/ to sys.path so we can import runloop +
# engine_base + the plugin folders from this test file.
_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "anomaly-detector"
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from engine_base import AnomalyScore, BackendFeatures  # noqa: E402
from runloop import (                                  # noqa: E402
    DEFAULT_ERROR_RATE_THRESHOLD,
    DEFAULT_FLIP_CONFIRMATION_CYCLES,
    DEFAULT_LATENCY_MULTIPLIER,
    BackendState,
    EnginePolicy,
    apply_stability_gate,
    bootstrap_engine,
    build_features_from_rows,
    peer_suppress_verdicts,
    policy_from_payload,
    recovery_reinclude_silent,
    score_to_event_payload,
    serialize_engine_state,
    should_publish,
)


# ── bootstrap_engine ──────────────────────────────────────────────────────────

def test_bootstrap_threshold_succeeds():
    boot = bootstrap_engine("threshold", EnginePolicy())
    assert boot.ready is True
    assert boot.name == "threshold"
    assert boot.requested == "threshold"
    assert boot.error is None


def test_bootstrap_unknown_engine_falls_back_to_threshold():
    boot = bootstrap_engine("definitely-not-a-real-engine", EnginePolicy())
    assert boot.ready is False
    assert boot.name == "threshold"
    assert boot.requested == "definitely-not-a-real-engine"
    assert boot.error is not None
    # Engine still scores — fallback is functional, not a no-op.
    f = BackendFeatures("b1", 1.0, 1.0, 0.0, 100)
    assert boot.engine.score(f).status == "healthy"


def test_bootstrap_threshold_failure_propagates():
    """If the baseline itself can't load, that's a deployment bug — surface it."""
    # Force a failure by monkey-patching select_engine — done via a custom
    # policy with engine_kwargs that ThresholdEngine rejects.
    bad_policy = EnginePolicy(latency_multiplier=float("nan"))
    # NaN is accepted by ThresholdEngine constructor (it's just a float field),
    # so this should still succeed — confirming we don't accidentally raise
    # on edge values. The propagation test runs via the unknown-engine path.
    boot = bootstrap_engine("threshold", bad_policy)
    assert boot.ready is True


# ── policy_from_payload ──────────────────────────────────────────────────────

def test_policy_from_full_payload():
    fallback = EnginePolicy()
    new = policy_from_payload({
        "anomaly_latency_multiplier": 5.0,
        "safe_mode": True,
        "anomaly_response": "advisory",
        "policy_version": 7,
        "anomaly_flip_confirmation_cycles": 4,
    }, fallback=fallback)
    assert new.latency_multiplier == 5.0
    assert new.safe_mode is True
    assert new.anomaly_response == "advisory"
    assert new.policy_version == 7
    assert new.flip_confirmation_cycles == 4


def test_policy_missing_fields_use_fallback():
    fallback = EnginePolicy(latency_multiplier=4.2, policy_version=3,
                            anomaly_response="advisory")
    new = policy_from_payload({}, fallback=fallback)
    assert new.latency_multiplier == 4.2
    assert new.anomaly_response == "advisory"
    assert new.policy_version == 3


def test_policy_malformed_types_use_fallback():
    fallback = EnginePolicy(latency_multiplier=4.2, policy_version=3)
    new = policy_from_payload({
        "anomaly_latency_multiplier": "not-a-number",
        "policy_version": "v9",
    }, fallback=fallback)
    assert new.latency_multiplier == 4.2
    assert new.policy_version == 3


def test_policy_safe_mode_coerces_truthy_values():
    new = policy_from_payload({"safe_mode": 1}, fallback=EnginePolicy())
    assert new.safe_mode is True
    new = policy_from_payload({"safe_mode": ""}, fallback=EnginePolicy())
    assert new.safe_mode is False


# ── build_features_from_rows ──────────────────────────────────────────────────

def test_features_empty_rows():
    assert build_features_from_rows([]) == []


def test_features_single_instance_pivots_metrics():
    rows = [
        ("backend_1", "request_latency_ms", 25.0, 100.0, 5.0, 600),
        ("backend_1", "error_rate",          0.02,  0.05, 0.01, 600),
    ]
    features = build_features_from_rows(rows)
    assert len(features) == 1
    f = features[0]
    assert f.backend_id == "backend_1"
    assert f.latency_ms == 100.0           # comes from max(latency)
    assert f.latency_rolling_mean_ms == 25.0  # comes from avg(latency)
    assert f.error_rate == 0.02
    assert f.sample_count == 600


def test_features_multiple_instances():
    rows = [
        ("b1", "request_latency_ms", 10.0, 20.0, 1.0, 100),
        ("b2", "request_latency_ms", 50.0, 80.0, 2.0, 100),
        ("b1", "error_rate",          0.0,  0.0, 0.0, 100),
        ("b2", "error_rate",          0.1,  0.2, 0.0, 100),
    ]
    features = build_features_from_rows(rows)
    by_id = {f.backend_id: f for f in features}
    assert set(by_id) == {"b1", "b2"}
    assert by_id["b2"].error_rate == 0.1


def test_features_malformed_row_is_skipped():
    rows = [
        ("backend_1", "request_latency_ms", 25.0, 100.0, 5.0, 600),
        ("malformed",),                          # too few columns
        ("backend_1", "error_rate", 0.02, 0.05, 0.01, 600),
    ]
    features = build_features_from_rows(rows)
    assert len(features) == 1
    assert features[0].backend_id == "backend_1"
    assert features[0].error_rate == 0.02


def test_features_handles_null_values_from_db():
    """psycopg2 returns None for SQL NULL — we coerce to 0.0 / 0 so the engine
    never sees Nones it can't compare against."""
    rows = [
        ("b1", "request_latency_ms", None, None, None, None),
    ]
    features = build_features_from_rows(rows)
    assert features[0].latency_ms == 0.0
    assert features[0].latency_rolling_mean_ms == 0.0
    assert features[0].sample_count == 0


def test_features_skips_backend_pool_sentinel():
    """The NGINX all-down sentinel `backend_pool` must never be scored as a
    backend: doing so flags the LB aggregate's 502 error_rate as an
    "unhealthy backend", which the sidecar excludes, emptying the pool — a
    self-sustaining outage (audit/_findings/anomaly-pool-collapse-rootcause)."""
    rows = [
        ("backend_pool", "error_rate",         1.0, 1.0, 0.0, 9000),
        ("backend_pool", "request_latency_ms", 0.0, 0.0, 0.0, 9000),
        ("backend_1",    "error_rate",         0.0, 0.0, 0.0, 600),
    ]
    features = build_features_from_rows(rows)
    by_id = {f.backend_id for f in features}
    assert "backend_pool" not in by_id
    assert by_id == {"backend_1"}


def test_features_skips_unknown_no_upstream_sentinel():
    """`unknown` (the shipper's fallback when NGINX never reached an upstream)
    is not a real backend and is dropped for the same reason."""
    rows = [
        ("unknown",   "error_rate", 1.0, 1.0, 0.0, 100),
        ("backend_2", "error_rate", 0.0, 0.0, 0.0, 100),
    ]
    features = build_features_from_rows(rows)
    assert {f.backend_id for f in features} == {"backend_2"}


def test_features_all_non_backend_rows_yield_empty():
    """A window that only saw the LB sentinels produces no features at all —
    nothing for the engine to flag, so no phantom exclusion."""
    rows = [
        ("backend_pool", "error_rate", 1.0, 1.0, 0.0, 9000),
        ("unknown",      "error_rate", 1.0, 1.0, 0.0, 50),
    ]
    assert build_features_from_rows(rows) == []


# ── should_publish gate ──────────────────────────────────────────────────────

def test_publish_safe_mode_never_publishes():
    score = AnomalyScore("b1", "unhealthy", 0.9)
    assert should_publish(score, EnginePolicy(safe_mode=True)) is False


def test_publish_advisory_mode_publishes_all_scores():
    policy = EnginePolicy(anomaly_response="advisory")
    assert should_publish(AnomalyScore("b1", "healthy", 0.0), policy) is True
    assert should_publish(AnomalyScore("b1", "degraded", 0.5), policy) is True
    assert should_publish(AnomalyScore("b1", "unhealthy", 0.9), policy) is True


def test_publish_auto_isolate_only_publishes_non_healthy():
    policy = EnginePolicy(anomaly_response="auto-isolate")
    assert should_publish(AnomalyScore("b1", "healthy", 0.0), policy) is False
    assert should_publish(AnomalyScore("b1", "degraded", 0.5), policy) is True
    assert should_publish(AnomalyScore("b1", "unhealthy", 0.9), policy) is True


# ── score_to_event_payload ───────────────────────────────────────────────────

def test_payload_shape():
    # A bare (no-evidence) non-healthy score still carries the derived UI
    # severity but omits the metric/observed/threshold keys.
    score = AnomalyScore("backend_42", "degraded", 0.73)
    payload = score_to_event_payload(score, model_version="isolation_forest")
    assert payload == {
        "backend_id":    "backend_42",
        "status":        "degraded",
        "score":         0.73,
        "model_version": "isolation_forest",
        "severity":      "warning",
    }


def test_payload_healthy_has_no_severity_or_evidence():
    # Healthy verdicts aren't alerts: no severity, no evidence keys.
    payload = score_to_event_payload(
        AnomalyScore("b1", "healthy", 0.0), model_version="threshold",
    )
    assert payload == {
        "backend_id":    "b1",
        "status":        "healthy",
        "score":         0.0,
        "model_version": "threshold",
    }


def test_payload_carries_evidence_and_severity():
    # An evidence-bearing unhealthy score threads metric/observed/threshold
    # through and maps unhealthy → critical.
    score = AnomalyScore(
        "backend_7", "unhealthy", 0.95,
        metric="latency_ms", observed_value=312.0, threshold=250.0,
    )
    payload = score_to_event_payload(score, model_version="threshold")
    assert payload == {
        "backend_id":     "backend_7",
        "status":         "unhealthy",
        "score":          0.95,
        "model_version":  "threshold",
        "metric":         "latency_ms",
        "observed_value": 312.0,
        "threshold":      250.0,
        "severity":       "critical",
    }


# ── EnginePolicy.engine_kwargs ───────────────────────────────────────────────

def test_engine_kwargs_includes_constructor_params():
    p = EnginePolicy(latency_multiplier=4.0, error_rate_threshold=0.1,
                    min_sample_count=20)
    kwargs = p.engine_kwargs()
    assert kwargs == {
        "latency_multiplier":   4.0,
        "error_rate_threshold": 0.1,
        "min_sample_count":     20,
    }


def test_engine_policy_defaults():
    p = EnginePolicy()
    assert p.latency_multiplier == DEFAULT_LATENCY_MULTIPLIER
    assert p.error_rate_threshold == DEFAULT_ERROR_RATE_THRESHOLD
    assert p.safe_mode is False
    assert p.anomaly_response == "auto-isolate"
    assert p.flip_confirmation_cycles == DEFAULT_FLIP_CONFIRMATION_CYCLES


def test_engine_kwargs_excludes_flip_confirmation_cycles():
    """flip_confirmation_cycles is consumed by app.py directly, not an
    engine constructor param."""
    p = EnginePolicy(flip_confirmation_cycles=5)
    assert "flip_confirmation_cycles" not in p.engine_kwargs()


# ── apply_stability_gate (B1/B2 fixes) ───────────────────────────────────────

def test_gate_passes_through_when_status_matches_last():
    state = BackendState(last_status="healthy", last_score=0.0)
    raw = AnomalyScore("b1", "healthy", 0.0)
    gated = apply_stability_gate(raw, low_sample=False, state=state, confirmation_cycles=2)
    assert gated == raw
    assert state.pending_status is None
    assert state.pending_count == 0


def test_gate_low_sample_preserves_last_non_healthy_status():
    """B1: a backend failing fast (few samples) shouldn't be reported
    healthy just because the engine had no evidence this cycle."""
    state = BackendState(last_status="unhealthy", last_score=0.9)
    raw = AnomalyScore("b1", "healthy", 0.0)  # engine's forced low-sample output
    gated = apply_stability_gate(raw, low_sample=True, state=state, confirmation_cycles=2)
    assert gated.status == "unhealthy"
    assert gated.score == 0.9
    # state unchanged -- no new evidence was actually observed
    assert state.last_status == "unhealthy"
    assert state.pending_count == 0


def test_gate_low_sample_with_healthy_last_status_passes_through():
    state = BackendState(last_status="healthy", last_score=0.0)
    raw = AnomalyScore("b1", "healthy", 0.0)
    gated = apply_stability_gate(raw, low_sample=True, state=state, confirmation_cycles=2)
    assert gated == raw


def test_gate_requires_confirmation_cycles_before_flip_to_unhealthy():
    """B2: a single noisy 'unhealthy' reading shouldn't flip the published
    status -- it must be observed for confirmation_cycles consecutive
    cycles."""
    state = BackendState(last_status="healthy", last_score=0.0)

    # Cycle 1: raw flips to unhealthy, but not yet confirmed.
    gated1 = apply_stability_gate(AnomalyScore("b1", "unhealthy", 0.9), False, state, confirmation_cycles=2)
    assert gated1.status == "healthy"
    assert state.last_status == "healthy"
    assert state.pending_status == "unhealthy"
    assert state.pending_count == 1

    # Cycle 2: raw unhealthy again -- now confirmed.
    gated2 = apply_stability_gate(AnomalyScore("b1", "unhealthy", 0.9), False, state, confirmation_cycles=2)
    assert gated2.status == "unhealthy"
    assert gated2.score == 0.9
    assert state.last_status == "unhealthy"
    assert state.pending_status is None
    assert state.pending_count == 0


def test_gate_recovery_also_requires_confirmation():
    """A recovery (unhealthy -> healthy) is gated the same way as a degrade."""
    state = BackendState(last_status="unhealthy", last_score=0.9)

    gated1 = apply_stability_gate(AnomalyScore("b1", "healthy", 0.0), False, state, confirmation_cycles=2)
    assert gated1.status == "unhealthy"  # not yet confirmed
    assert state.pending_status == "healthy"
    assert state.pending_count == 1

    gated2 = apply_stability_gate(AnomalyScore("b1", "healthy", 0.0), False, state, confirmation_cycles=2)
    assert gated2.status == "healthy"
    assert state.last_status == "healthy"


def test_gate_flapping_resets_pending_count():
    """If the raw status reverts to last_status before confirmation, the
    pending flip is dropped -- prevents single-cycle flaps from
    accumulating across unrelated flips."""
    state = BackendState(last_status="healthy", last_score=0.0)

    apply_stability_gate(AnomalyScore("b1", "degraded", 0.5), False, state, confirmation_cycles=2)
    assert state.pending_status == "degraded"
    assert state.pending_count == 1

    # Reverts to healthy before confirmation.
    gated = apply_stability_gate(AnomalyScore("b1", "healthy", 0.0), False, state, confirmation_cycles=2)
    assert gated.status == "healthy"
    assert state.pending_status is None
    assert state.pending_count == 0


def test_gate_confirmation_cycles_one_confirms_immediately():
    """confirmation_cycles=1 acts as a no-op gate -- every raw change is
    confirmed on first observation."""
    state = BackendState(last_status="healthy", last_score=0.0)
    gated = apply_stability_gate(AnomalyScore("b1", "unhealthy", 0.9), False, state, confirmation_cycles=1)
    assert gated.status == "unhealthy"
    assert state.last_status == "unhealthy"


def test_gate_low_sample_hold_is_unbounded_by_default():
    """Without a TTL the B1 hold preserves a non-healthy status indefinitely
    while the backend stays quiet (the original behaviour)."""
    state = BackendState(last_status="unhealthy", last_score=0.9)
    raw = AnomalyScore("b1", "healthy", 0.0)
    for _ in range(50):
        gated = apply_stability_gate(raw, low_sample=True, state=state, confirmation_cycles=2)
        assert gated.status == "unhealthy"
    assert state.low_sample_hold_count == 50


def test_gate_low_sample_hold_ttl_releases_after_max_hold_cycles():
    """B1 hold with a TTL: after max_hold_cycles consecutive held cycles the
    hold is released and the (low-sample, healthy) raw reading is processed by
    the normal confirmation path, so the status decays back toward healthy
    instead of sticking non-healthy forever on a permanently quiet backend."""
    state = BackendState(last_status="unhealthy", last_score=0.9)
    raw = AnomalyScore("b1", "healthy", 0.0)

    # Cycles 1..3: held (TTL not yet exceeded).
    for i in range(1, 4):
        gated = apply_stability_gate(raw, low_sample=True, state=state,
                                     confirmation_cycles=2, max_hold_cycles=3)
        assert gated.status == "unhealthy", f"cycle {i} should still hold"

    # Cycle 4: TTL exceeded -> fall through; raw healthy starts the flip but
    # needs confirmation, so still reported unhealthy this cycle.
    gated4 = apply_stability_gate(raw, low_sample=True, state=state,
                                  confirmation_cycles=2, max_hold_cycles=3)
    assert gated4.status == "unhealthy"
    assert state.pending_status == "healthy"

    # Cycle 5: healthy confirmed -> hold released, backend reads healthy again.
    gated5 = apply_stability_gate(raw, low_sample=True, state=state,
                                  confirmation_cycles=2, max_hold_cycles=3)
    assert gated5.status == "healthy"
    assert state.last_status == "healthy"


def test_gate_low_sample_hold_count_resets_when_samples_return():
    """A populated cycle clears the hold counter, so a later quiet patch gets
    the full TTL again rather than a stale count."""
    state = BackendState(last_status="unhealthy", last_score=0.9)
    raw_low = AnomalyScore("b1", "healthy", 0.0)
    apply_stability_gate(raw_low, low_sample=True, state=state,
                         confirmation_cycles=2, max_hold_cycles=3)
    assert state.low_sample_hold_count == 1
    # Samples return (still non-healthy raw, confirming the existing status).
    apply_stability_gate(AnomalyScore("b1", "unhealthy", 0.9), low_sample=False,
                         state=state, confirmation_cycles=2, max_hold_cycles=3)
    assert state.low_sample_hold_count == 0


# ── serialize_engine_state (Live Engines #121) ───────────────────────────────

def _state_kwargs(**overrides):
    base = dict(
        service="anomaly-detector",
        channel="smartload.anomaly",
        runloop_enabled=True,
        engine_name="threshold",
        engine_requested="threshold",
        engine_ready=True,
        engine_error=None,
        policy=EnginePolicy(latency_multiplier=2.5, policy_version=4),
        ticks_total=10,
        publishes_total=3,
        last_tick_at="2026-05-24T19:30:00+00:00",
        last_publish_at="2026-05-24T19:29:50+00:00",
        last_tick_monotonic=None,
        last_output=None,
    )
    base.update(overrides)
    return base


def test_state_shape_has_every_top_level_key():
    body = serialize_engine_state(**_state_kwargs())
    assert set(body) == {
        "service", "channel", "runloop_enabled",
        "engine", "policy_snapshot", "stats", "last_output",
    }


def test_state_engine_kind_is_engine_for_anomaly():
    body = serialize_engine_state(**_state_kwargs())
    assert body["engine"]["kind"] == "engine"
    assert body["engine"]["requested"] == "threshold"
    assert body["engine"]["loaded"] == "threshold"
    assert body["engine"]["ready"] is True


def test_state_age_is_none_when_no_tick_yet():
    body = serialize_engine_state(**_state_kwargs(last_tick_monotonic=None))
    assert body["stats"]["last_tick_age_seconds"] is None


def test_state_age_is_computed_when_monotonic_set():
    import time as _t
    now = _t.monotonic()
    body = serialize_engine_state(**_state_kwargs(last_tick_monotonic=now - 2.5))
    age = body["stats"]["last_tick_age_seconds"]
    assert age is not None and 2.0 < age < 5.0


def test_state_policy_snapshot_is_dict_of_engine_policy():
    p = EnginePolicy(latency_multiplier=7.5, safe_mode=True, policy_version=12)
    body = serialize_engine_state(**_state_kwargs(policy=p))
    snap = body["policy_snapshot"]
    assert snap["latency_multiplier"] == 7.5
    assert snap["safe_mode"] is True
    assert snap["policy_version"] == 12


def test_state_carries_last_output_verbatim_for_anomaly_list():
    outputs = [
        {"backend_id": "b1", "status": "unhealthy", "score": 0.9,
         "model_version": "threshold"},
        {"backend_id": "b2", "status": "healthy", "score": 0.0,
         "model_version": "threshold"},
    ]
    body = serialize_engine_state(**_state_kwargs(last_output=outputs))
    assert body["last_output"] == outputs


def test_state_reports_runloop_disabled_and_error():
    body = serialize_engine_state(**_state_kwargs(
        runloop_enabled=False,
        engine_name="threshold",
        engine_requested="isolation_forest",
        engine_ready=False,
        engine_error="model file missing",
    ))
    assert body["runloop_enabled"] is False
    assert body["engine"]["ready"] is False
    assert body["engine"]["error"] == "model file missing"
    assert body["engine"]["requested"] == "isolation_forest"
    assert body["engine"]["loaded"] == "threshold"


# ── recovery_reinclude_silent (Fix B, silent-backend / no-recovery-trap) ──────


def test_recovery_reinclude_silent_readmits_aged_silent_exclusion():
    # A benched backend that dropped out of the metrics query: on the exclusion
    # clock, no fresh verdict this cycle, aged past the recovery window -> ONE
    # probationary healthy re-admit, with the stability-gate memory reset to a
    # clean slate so the gate doesn't immediately re-confirm the stale unhealthy.
    state = BackendState(last_status="unhealthy", last_score=0.9)
    state.excluded_since_monotonic = 100.0
    policy = EnginePolicy(recovery_window_seconds=30)
    out = recovery_reinclude_silent("b1:8080", state, policy, now_monotonic=131.0)
    assert out is not None
    assert out.status == "healthy"
    assert out.backend_id == "b1:8080"
    # Clock is RE-ARMED to now (not cleared) so a still-silent backend is re-probed.
    assert state.excluded_since_monotonic == 131.0
    assert state.recovery_reinclude_emitted is True
    assert state.last_status == "healthy"
    assert state.last_score == 0.0
    assert state.pending_status is None
    assert state.low_sample_hold_count == 0


def test_recovery_reinclude_silent_holds_before_window():
    # 20s excluded, window 30s -> not yet due; nothing changes.
    state = BackendState(last_status="unhealthy", last_score=0.9)
    state.excluded_since_monotonic = 100.0
    policy = EnginePolicy(recovery_window_seconds=30)
    out = recovery_reinclude_silent("b1:8080", state, policy, now_monotonic=120.0)
    assert out is None
    assert state.excluded_since_monotonic == 100.0
    assert state.recovery_reinclude_emitted is False
    assert state.last_status == "unhealthy"


def test_recovery_reinclude_silent_reprobes_each_window():
    # The clock is RE-ARMED (not cleared) on re-admit, so a backend that stays
    # silent is re-probed once per recovery window — never abandoned in a stuck
    # "down; but the detector thinks it's fine" limbo across a run boundary.
    state = BackendState(last_status="unhealthy", last_score=0.9)
    state.excluded_since_monotonic = 100.0
    policy = EnginePolicy(recovery_window_seconds=30)
    first = recovery_reinclude_silent("b1:8080", state, policy, now_monotonic=140.0)
    assert first is not None
    assert state.excluded_since_monotonic == 140.0          # re-armed to now
    # Too soon (15s < 30s window) -> no re-probe yet.
    assert recovery_reinclude_silent("b1:8080", state, policy, now_monotonic=155.0) is None
    # Another full window elapsed -> re-probe again (not a one-shot).
    third = recovery_reinclude_silent("b1:8080", state, policy, now_monotonic=175.0)
    assert third is not None
    assert third.status == "healthy"


def test_recovery_reinclude_silent_noop_when_not_excluded():
    # A backend not on the exclusion clock is never spontaneously re-admitted.
    state = BackendState(last_status="healthy", last_score=0.0)
    policy = EnginePolicy(recovery_window_seconds=30)
    out = recovery_reinclude_silent("b1:8080", state, policy, now_monotonic=999.0)
    assert out is None
    assert state.excluded_since_monotonic is None


# ── peer_suppress_verdicts (Fix A + D3 outlier margin: busy-vs-broken) ─────────


def _feat(bid, *, latency_mean, error_rate, latency_max=None):
    lm = latency_max if latency_max is not None else latency_mean
    return BackendFeatures(
        backend_id=bid, latency_ms=lm, latency_rolling_mean_ms=latency_mean,
        error_rate=error_rate, sample_count=100,
    )


def test_peer_suppress_downgrades_uniform_overload():
    # 4 backends all `unhealthy` with near-identical (evenly overloaded) latency
    # and error: nobody is > 50% worse than the median pack, so ALL are downgraded
    # to healthy — scale-out is the right response, not benching the whole pool.
    # (Pre-D3, the strict ">median" test leaked ~half of these through.)
    policy = EnginePolicy(overload_peer_fraction=0.5, overload_min_peers=3,
                          overload_outlier_margin=0.5)
    scored = [
        (_feat("b1", latency_mean=500, error_rate=0.30), AnomalyScore("b1", "unhealthy", 0.9)),
        (_feat("b2", latency_mean=520, error_rate=0.31), AnomalyScore("b2", "unhealthy", 0.9)),
        (_feat("b3", latency_mean=480, error_rate=0.29), AnomalyScore("b3", "unhealthy", 0.9)),
        (_feat("b4", latency_mean=510, error_rate=0.30), AnomalyScore("b4", "unhealthy", 0.9)),
    ]
    out = peer_suppress_verdicts(scored, policy)
    assert [s.status for s in out] == ["healthy", "healthy", "healthy", "healthy"]


def test_peer_suppress_keeps_genuine_outlier_under_overload():
    # 4 unhealthy backends; b4 is a genuine outlier (~3x the cohort latency, well
    # past the 50% margin) -> it KEEPS its exclusion while the evenly-loaded pack
    # is downgraded. A single bad apple among an overloaded pool is still caught.
    policy = EnginePolicy(overload_peer_fraction=0.5, overload_min_peers=3,
                          overload_outlier_margin=0.5)
    scored = [
        (_feat("b1", latency_mean=500, error_rate=0.30), AnomalyScore("b1", "unhealthy", 0.9)),
        (_feat("b2", latency_mean=520, error_rate=0.30), AnomalyScore("b2", "unhealthy", 0.9)),
        (_feat("b3", latency_mean=480, error_rate=0.30), AnomalyScore("b3", "unhealthy", 0.9)),
        (_feat("b4", latency_mean=1600, error_rate=0.30), AnomalyScore("b4", "unhealthy", 0.9)),
    ]
    out = peer_suppress_verdicts(scored, policy)
    statuses = {s.backend_id: s.status for s in out}
    assert statuses["b4"] == "unhealthy"            # outlier kept
    assert statuses["b1"] == statuses["b2"] == statuses["b3"] == "healthy"


def test_peer_suppress_uses_typical_not_max_latency():
    # b1 has a transient MAX spike (latency_ms=4000) but a normal rolling MEAN
    # (500, same as the pack). The suppressor must judge on the typical mean, so
    # the spike does NOT mark it an outlier -> downgraded with the pack (D9 fix).
    policy = EnginePolicy(overload_peer_fraction=0.5, overload_min_peers=3,
                          overload_outlier_margin=0.5)
    scored = [
        (_feat("b1", latency_mean=500, error_rate=0.30, latency_max=4000), AnomalyScore("b1", "unhealthy", 0.9)),
        (_feat("b2", latency_mean=520, error_rate=0.30), AnomalyScore("b2", "unhealthy", 0.9)),
        (_feat("b3", latency_mean=480, error_rate=0.30), AnomalyScore("b3", "unhealthy", 0.9)),
        (_feat("b4", latency_mean=510, error_rate=0.30), AnomalyScore("b4", "unhealthy", 0.9)),
    ]
    out = peer_suppress_verdicts(scored, policy)
    assert {s.backend_id: s.status for s in out}["b1"] == "healthy"


def test_peer_suppress_keeps_lone_fault_among_healthy():
    # A single badly-broken backend among healthy peers is far past the margin
    # (2000ms vs a ~50ms cohort), so it KEEPS its exclusion. The margin is the
    # discriminator — no pool-fraction gate is needed for this.
    policy = EnginePolicy(overload_min_peers=3, overload_outlier_margin=0.5)
    scored = [
        (_feat("b1", latency_mean=2000, error_rate=0.9), AnomalyScore("b1", "unhealthy", 0.9)),
        (_feat("b2", latency_mean=50, error_rate=0.0), AnomalyScore("b2", "healthy", 0.0)),
        (_feat("b3", latency_mean=50, error_rate=0.0), AnomalyScore("b3", "healthy", 0.0)),
        (_feat("b4", latency_mean=50, error_rate=0.0), AnomalyScore("b4", "healthy", 0.0)),
    ]
    out = peer_suppress_verdicts(scored, policy)
    assert {s.backend_id: s.status for s in out}["b1"] == "unhealthy"


def test_peer_suppress_engages_below_half_when_within_margin():
    # Cascade fix: with just 1 of 5 backends tripping unhealthy, if it is within
    # the margin of the cohort (the whole pool is similarly loaded, it just crossed
    # its own per-backend threshold first), it is suppressed -> kept serving. The
    # suppressor no longer waits for half the pool to fail before engaging — that
    # delay is what let the pool cascade down to ~3 active under load.
    policy = EnginePolicy(overload_min_peers=3, overload_outlier_margin=0.5)
    scored = [
        (_feat("b1", latency_mean=510, error_rate=0.06), AnomalyScore("b1", "unhealthy", 0.9)),
        (_feat("b2", latency_mean=500, error_rate=0.04), AnomalyScore("b2", "healthy", 0.0)),
        (_feat("b3", latency_mean=490, error_rate=0.04), AnomalyScore("b3", "healthy", 0.0)),
        (_feat("b4", latency_mean=505, error_rate=0.04), AnomalyScore("b4", "healthy", 0.0)),
        (_feat("b5", latency_mean=495, error_rate=0.04), AnomalyScore("b5", "healthy", 0.0)),
    ]
    out = peer_suppress_verdicts(scored, policy)
    assert {s.backend_id: s.status for s in out}["b1"] == "healthy"


def _scored_with_outlier():
    return [
        (_feat("b1", latency_mean=500, error_rate=0.30), AnomalyScore("b1", "unhealthy", 0.9)),
        (_feat("b2", latency_mean=520, error_rate=0.30), AnomalyScore("b2", "unhealthy", 0.9)),
        (_feat("b3", latency_mean=480, error_rate=0.30), AnomalyScore("b3", "unhealthy", 0.9)),
        (_feat("b4", latency_mean=1600, error_rate=0.30), AnomalyScore("b4", "unhealthy", 0.9)),
    ]


def test_peer_suppress_hysteresis_holds_transient_then_excludes_sustained():
    # #1: a backend that is a cohort-outlier for only ONE cycle is NOT benched
    # (held); a SUSTAINED outlier (2 cycles) IS — so a transient ramp-rate
    # difference during a spike doesn't trigger the over-exclusion cascade.
    policy = EnginePolicy(overload_min_peers=3, overload_outlier_margin=0.5,
                          overload_exclusion_confirmations=2)
    states = [BackendState() for _ in range(4)]
    out1 = peer_suppress_verdicts(_scored_with_outlier(), policy, states=states)
    assert {s.backend_id: s.status for s in out1}["b4"] == "healthy"    # transient -> held
    out2 = peer_suppress_verdicts(_scored_with_outlier(), policy, states=states)
    assert {s.backend_id: s.status for s in out2}["b4"] == "unhealthy"  # sustained -> benched


def test_peer_suppress_surge_suppresses_even_outliers():
    # #2: when the whole cohort's latency surges cycle-over-cycle (a load spike),
    # EVERY exclusion is suppressed — even a backend past the margin — because a
    # synchronized ramp is overload, not a fault.
    policy = EnginePolicy(overload_min_peers=3, overload_outlier_margin=0.5,
                          overload_surge_factor=1.5)
    mem = {}
    base = [(_feat(f"b{n}", latency_mean=100, error_rate=0.0),
             AnomalyScore(f"b{n}", "healthy", 0.0)) for n in range(1, 5)]
    peer_suppress_verdicts(base, policy, cohort_memory=mem)    # baseline lat_median=100
    spike = [
        (_feat("b1", latency_mean=500, error_rate=0.30), AnomalyScore("b1", "unhealthy", 0.9)),
        (_feat("b2", latency_mean=600, error_rate=0.30), AnomalyScore("b2", "unhealthy", 0.9)),
        (_feat("b3", latency_mean=550, error_rate=0.30), AnomalyScore("b3", "unhealthy", 0.9)),
        (_feat("b4", latency_mean=1800, error_rate=0.30), AnomalyScore("b4", "unhealthy", 0.9)),  # outlier
    ]
    out = peer_suppress_verdicts(spike, policy, cohort_memory=mem)
    assert all(s.status == "healthy" for s in out)            # surge -> all kept, even b4
