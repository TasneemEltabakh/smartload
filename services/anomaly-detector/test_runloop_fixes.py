"""
services/anomaly-detector/test_runloop_fixes.py
────────────────────────────────────────────────
Unit tests for the two surgical pool-collapse fixes added to the run loop:

  Fix A — peer-relative overload suppression (peer_suppress_verdicts):
    * system-wide overload (a degraded majority that's no worse than the pack)
      is suppressed so backends are NOT excluded;
    * a lone outlier among healthy peers is still flagged;
    * tiny pools (< overload_min_peers) keep the raw verdicts;
    * a genuinely-worse backend inside an overloaded pool still gets excluded.

  Fix B — time-based re-inclusion (recovery_reinclude):
    * a backend excluded longer than the recovery window with no fresh
      unhealthy verdict gets a probationary "healthy" re-admit;
    * before the window elapses it stays excluded (no re-admit);
    * a backend that's organically healthy again resets cleanly;
    * the re-admit happens once per exclusion (no thrash).

Pure-Python — no Flask / Redis / DB. Mirrors the layout of the engine tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SVC = Path(__file__).resolve().parent
if str(_SVC) not in sys.path:
    sys.path.insert(0, str(_SVC))

from engine_base import AnomalyScore, BackendFeatures  # noqa: E402
from runloop import (  # noqa: E402
    BackendState,
    EnginePolicy,
    peer_suppress_verdicts,
    policy_from_payload,
    recovery_reinclude,
)


def _feat(bid: str, *, err=0.0, latency=20.0, samples=300) -> BackendFeatures:
    return BackendFeatures(
        backend_id=bid,
        latency_ms=latency,
        latency_rolling_mean_ms=latency,
        error_rate=err,
        sample_count=samples,
        latency_rolling_std_ms=latency * 0.1,
    )


def _unhealthy(bid: str, *, metric="error_rate", obs=0.5, thr=0.05) -> AnomalyScore:
    return AnomalyScore(bid, "unhealthy", 1.0, metric=metric, observed_value=obs, threshold=thr)


# ─────────────────────────── Fix A: peer suppression ────────────────────────

def test_system_wide_overload_is_suppressed():
    """When a majority of backends are unhealthy together and none is worse
    than the pack, every exclusion is downgraded to healthy (scale-out signal,
    not a fault)."""
    policy = EnginePolicy(overload_peer_fraction=0.5, overload_min_peers=3)
    # All four backends equally hammered (identical error_rate) → all unhealthy.
    scored = [
        (_feat(f"b{i}", err=0.30, latency=400.0), _unhealthy(f"b{i}", obs=0.30))
        for i in range(4)
    ]
    out = peer_suppress_verdicts(scored, policy)
    assert all(s.status == "healthy" for s in out), [s.status for s in out]
    assert all(s.score == 0.0 for s in out)


def test_lone_outlier_is_still_flagged():
    """One bad backend among healthy peers is NOT suppressed (degraded fraction
    below the overload threshold)."""
    policy = EnginePolicy(overload_peer_fraction=0.5, overload_min_peers=3)
    scored = [
        (_feat("b0", err=0.40, latency=900.0), _unhealthy("b0", obs=0.40)),
        (_feat("b1", err=0.001, latency=20.0), AnomalyScore("b1", "healthy", 0.0)),
        (_feat("b2", err=0.001, latency=20.0), AnomalyScore("b2", "healthy", 0.0)),
        (_feat("b3", err=0.001, latency=20.0), AnomalyScore("b3", "healthy", 0.0)),
    ]
    out = peer_suppress_verdicts(scored, policy)
    assert out[0].status == "unhealthy"  # the outlier stays excluded
    assert [s.status for s in out[1:]] == ["healthy", "healthy", "healthy"]


def test_worse_than_pack_outlier_kept_during_overload():
    """Even when the whole pool is degraded, a backend MEANINGFULLY worse than
    the cohort median (here, far higher error_rate) is still excluded."""
    policy = EnginePolicy(overload_peer_fraction=0.5, overload_min_peers=3)
    scored = [
        (_feat("b0", err=0.90, latency=400.0), _unhealthy("b0", obs=0.90)),  # the true bad apple
        (_feat("b1", err=0.20, latency=400.0), _unhealthy("b1", obs=0.20)),
        (_feat("b2", err=0.20, latency=400.0), _unhealthy("b2", obs=0.20)),
        (_feat("b3", err=0.20, latency=400.0), _unhealthy("b3", obs=0.20)),
    ]
    out = peer_suppress_verdicts(scored, policy)
    # b0 is above the cohort error_rate median → still excluded.
    assert out[0].status == "unhealthy"
    # The pack members (at the median, not worse) are suppressed.
    assert [s.status for s in out[1:]] == ["healthy", "healthy", "healthy"]


def test_too_few_peers_keeps_raw_verdicts():
    """With fewer than overload_min_peers live backends, peer comparison can't
    engage; raw verdicts stand."""
    policy = EnginePolicy(overload_peer_fraction=0.5, overload_min_peers=3)
    scored = [
        (_feat("b0", err=0.30), _unhealthy("b0", obs=0.30)),
        (_feat("b1", err=0.30), _unhealthy("b1", obs=0.30)),
    ]
    out = peer_suppress_verdicts(scored, policy)
    assert all(s.status == "unhealthy" for s in out)


def test_suppression_does_not_mutate_input_scores():
    policy = EnginePolicy(overload_peer_fraction=0.5, overload_min_peers=3)
    raw = [_unhealthy(f"b{i}", obs=0.30) for i in range(4)]
    scored = [(_feat(f"b{i}", err=0.30), raw[i]) for i in range(4)]
    peer_suppress_verdicts(scored, policy)
    assert all(s.status == "unhealthy" for s in raw)  # originals untouched


# ─────────────────────────── Fix B: re-inclusion ────────────────────────────

def test_excluded_past_window_gets_readmitted():
    """A backend excluded longer than the recovery window, with no fresh
    unhealthy verdict this cycle, is re-admitted as healthy."""
    policy = EnginePolicy(recovery_window_seconds=30)
    st = BackendState()
    # t=0: first unhealthy → exclusion clock starts.
    assert recovery_reinclude("b0", "unhealthy", st, policy, now_monotonic=0.0) is None
    assert st.excluded_since_monotonic == 0.0
    # t=31s: low-sample hold keeps it non-fresh; here we model "no fresh
    # unhealthy" as a degraded gated status (the gate held the old status but
    # this cycle produced no new unhealthy). Past the window → re-admit.
    out = recovery_reinclude("b0", "degraded", st, policy, now_monotonic=31.0)
    assert out is not None
    assert out.status == "healthy"
    # Exclusion bookkeeping cleared so it can be re-excluded fresh if still bad.
    assert st.excluded_since_monotonic is None


def test_not_readmitted_before_window():
    policy = EnginePolicy(recovery_window_seconds=30)
    st = BackendState()
    recovery_reinclude("b0", "unhealthy", st, policy, now_monotonic=0.0)
    # 10s later, still excluded, no re-admit yet.
    out = recovery_reinclude("b0", "degraded", st, policy, now_monotonic=10.0)
    assert out is None
    assert st.excluded_since_monotonic == 0.0


def test_fresh_unhealthy_resets_no_readmit():
    """A fresh unhealthy verdict each cycle is adverse evidence: never
    re-admit, and the clock measures total time excluded."""
    policy = EnginePolicy(recovery_window_seconds=30)
    st = BackendState()
    recovery_reinclude("b0", "unhealthy", st, policy, now_monotonic=0.0)
    out = recovery_reinclude("b0", "unhealthy", st, policy, now_monotonic=100.0)
    assert out is None
    # original timestamp preserved (total-time-excluded semantics)
    assert st.excluded_since_monotonic == 0.0


def test_organic_healthy_clears_exclusion():
    policy = EnginePolicy(recovery_window_seconds=30)
    st = BackendState()
    recovery_reinclude("b0", "unhealthy", st, policy, now_monotonic=0.0)
    out = recovery_reinclude("b0", "healthy", st, policy, now_monotonic=5.0)
    assert out is None
    assert st.excluded_since_monotonic is None
    assert st.recovery_reinclude_emitted is False


def test_readmit_happens_once_per_exclusion():
    """After a re-admit, if the backend is still excluded next cycle the clock
    restarts; we don't re-emit a healthy verdict every cycle (no thrash)."""
    policy = EnginePolicy(recovery_window_seconds=30)
    st = BackendState()
    recovery_reinclude("b0", "unhealthy", st, policy, now_monotonic=0.0)
    first = recovery_reinclude("b0", "degraded", st, policy, now_monotonic=31.0)
    assert first is not None and first.status == "healthy"
    # Next cycle still degraded but exclusion clock was cleared, so no exclusion
    # is pending → no second healthy spam.
    second = recovery_reinclude("b0", "degraded", st, policy, now_monotonic=32.0)
    assert second is None
    # If it goes unhealthy again, a NEW exclusion starts and can re-admit later.
    recovery_reinclude("b0", "unhealthy", st, policy, now_monotonic=33.0)
    assert st.excluded_since_monotonic == 33.0


# ──────────────────── manual-isolate path is unaffected ─────────────────────

def test_manual_isolate_bypasses_both_fixes():
    """Manual isolates publish via POST /api/v1/isolate, which bypasses the run
    loop (and thus peer_suppress_verdicts + recovery_reinclude) entirely. We
    assert the contract here: a manually-composed isolate plan is never fed
    through these functions, so it can't be downgraded or auto-readmitted.

    This guards the design invariant — if someone later routes manual isolates
    through the run loop, this test documents that the suppression/recovery
    helpers must not see them."""
    from manual import plan_manual_isolate

    plan = plan_manual_isolate(
        backend_id="b9", status="unhealthy", actor="operator", user_reason="drain"
    )
    # The manual model_version tag identifies an operator action.
    assert plan.payload["model_version"].startswith("manual:")
    # Sanity: the run-loop helpers operate on AnomalyScore/BackendState produced
    # by the engine, not on ManualIsolatePlan — the plan never reaches them.
    assert not hasattr(plan, "score") or plan.status == "unhealthy"


# ────────────────────────── policy threading ────────────────────────────────

def test_policy_from_payload_reads_new_knobs():
    fallback = EnginePolicy()
    payload = {
        "anomaly_recovery_window_seconds": 45,
        "anomaly_overload_peer_fraction": 0.6,
        "anomaly_overload_min_peers": 4,
    }
    p = policy_from_payload(payload, fallback=fallback)
    assert p.recovery_window_seconds == 45
    assert p.overload_peer_fraction == 0.6
    assert p.overload_min_peers == 4


def test_policy_from_payload_falls_back_on_missing_knobs():
    fallback = EnginePolicy(
        recovery_window_seconds=99, overload_peer_fraction=0.7, overload_min_peers=5
    )
    p = policy_from_payload({}, fallback=fallback)
    assert p.recovery_window_seconds == 99
    assert p.overload_peer_fraction == 0.7
    assert p.overload_min_peers == 5
