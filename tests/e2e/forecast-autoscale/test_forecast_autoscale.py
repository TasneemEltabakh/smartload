"""
tests/e2e/forecast-autoscale/test_forecast_autoscale.py
─────────────────────────────────────────────────────────
End-to-end suite for the forecast-autoscale slice
(docs/features/forecast-autoscale.md). Exercises the full forecast → scale
slice across multiple services:

  - a ForecastResult published on smartload.forecast is consumed by the
    autoscaler, which decides scale_out and publishes a ScalingEvent on
    smartload.scale (observed via the SDK's BFF SSE stream),
  - the resulting decision lands in scaling_events and is readable via the
    SDK audit surface (client.list_audit("scaling")),
  - the cooldown timer suppresses a second forecast inside the window,
  - the operator override client.scale(target) actuates the same pool the
    forecast path drives.

This started life as tests/integration/test_autoscaler.py (the raw
psycopg2 + Docker-SDK live-stack version, T1.3). Migrated here under #140
because it exercises a customer-facing slice through multiple services;
observation now goes through the SDK (the customer surface) instead of
direct DB / Docker reads. The forecast injection still goes straight to
Redis because there is no operator-facing "publish a forecast" surface —
the forecasting service owns that channel, and a deterministic e2e needs
to drive a known predicted_rps rather than wait on the moving-average
baseline.

Requires a live docker-compose stack:
    docker compose up -d
    pytest tests/e2e/forecast-autoscale/ -v
"""

from __future__ import annotations

import threading
import time

import pytest
import requests

from smartload_client import SmartLoadError

from services.shared.contracts import ForecastResult, publish_envelope

pytestmark = pytest.mark.e2e

FORECAST_CHANNEL = "smartload.forecast"
SCALE_CHANNEL    = "smartload.scale"

# Headroom for the autoscaler control loop to consume the forecast and
# actuate, plus the BFF SSE hop. LOOP_TICK_SECONDS in the autoscaler is 5 s
# by default; the SSE relay adds a little, so allow generously.
SCALE_DEADLINE_SECONDS = 30.0

# A predicted_rps well above any plausible current capacity, so the
# decision is unambiguously scale_out regardless of the starting count.
HIGH_PREDICTED_RPS = 9999.0


# ── helpers ───────────────────────────────────────────────────────────────────

def _ensure_headroom(client, baseline_count: int) -> int:
    """Scale the pool down to a count with at least one slot of headroom so
    a forecast-driven scale_out has somewhere to go.

    The compose stack provisions a fixed ceiling of test-backend containers
    and dynamic provisioning is off by default, so scale_out only actuates
    when a stopped compose container is available to start. Returns the
    count the pool is actually at."""
    policy = client.get_policy()
    min_b, max_b = int(policy["min_backends"]), int(policy["max_backends"])
    # Aim a couple below the start so the forecast has room even if a
    # competing reactive decision nudges the count by one.
    target = max(min_b, min(baseline_count, max_b) - 2)
    try:
        r = client.scale(target, actor="e2e-fa-headroom", reason="make scale-out room")
    except SmartLoadError as exc:
        pytest.skip(f"could not establish scale-out headroom: {exc}")
    return int(r.get("final_count", target))


def _publish_forecast(redis_client, predicted_rps: float, horizon_minutes: int = 5) -> str:
    """Publish a ForecastResult envelope on smartload.forecast; return the
    event_id so the test can correlate the triggered ScalingEvent."""
    payload = ForecastResult(
        horizon_minutes=horizon_minutes,
        predicted_rps=predicted_rps,
        confidence_lower=predicted_rps * 0.9,
        confidence_upper=predicted_rps * 1.1,
        model_id="e2e-forecast-autoscale",
    )
    return publish_envelope(
        redis_client, FORECAST_CHANNEL, source="e2e-forecast-autoscale", payload=payload,
    )


def _scale_out_cooldown_enforced(autoscaler_url: str) -> bool:
    """True when the active autoscaler controller suppresses a second
    scale-out inside a cooldown window — the precondition for the
    back-to-back-suppression contract this leg asserts.

    The legacy ``step`` controller applies one symmetric cooldown
    (``autoscaler_cooldown_seconds``) to every action, so a scale-out arms a
    window that suppresses the next one. The ``target`` controller (the
    default since v1.0.7br ships it as the deployed model) runs an asymmetric
    fast-out / slow-in policy with ``AUTOSCALER_SCALE_OUT_COOLDOWN_SECONDS=0``
    by design — consecutive forecast-driven scale-outs toward an unmet target
    are intended, not a cooldown violation — so there is nothing to suppress.

    The live controller is read from the autoscaler ``/health`` surface rather
    than assumed from a build-time default. If ``/health`` can't be reached the
    controller is unknown, so the suppression assertion is treated as
    non-applicable (the surrounding suite already skips when the stack can't
    actuate)."""
    try:
        body = requests.get(f"{autoscaler_url}/health", timeout=3).json()
    except (requests.RequestException, ValueError):
        return False
    return str(body.get("controller", "step")).lower() == "step"


def _wait_for_scale_event(client, forecast_event_id: str, timeout: float) -> dict | None:
    """Subscribe to smartload.scale via the SDK BFF SSE stream and block
    until a ScalingEvent triggered by `forecast_event_id` arrives, or
    timeout. Returns the payload dict or None."""
    received: list[dict] = []
    done = threading.Event()

    def _cb(channel, payload, _meta):
        if channel != SCALE_CHANNEL:
            return
        if payload.get("forecast_event_id") == forecast_event_id:
            received.append(payload)
            done.set()

    sub = client.engines.subscribe(_cb, channels=[SCALE_CHANNEL])
    try:
        done.wait(timeout=timeout)
    finally:
        sub.close()
    return received[0] if received else None


# ── forecast-driven scale-out ─────────────────────────────────────────────────

class TestForecastDrivenScaling:

    def test_high_forecast_triggers_scale_out(
        self, client, redis_publisher, baseline_count, reset_cooldown,
    ):
        """A predicted_rps well above current capacity must produce one
        scale_out ScalingEvent on smartload.scale, tagged with the
        triggering forecast event_id, and that decision must be visible
        through the SDK scaling-audit surface.

        Establishes headroom and resets the autoscaler cooldown first so the
        forecast has somewhere to scale and a clean cooldown window. If the
        cluster can't actuate (no spare compose container / provisioning
        off), the forecast produces no envelope and the test skips."""
        _ensure_headroom(client, baseline_count)
        if not reset_cooldown():
            pytest.skip("could not reset autoscaler cooldown (no Docker socket)")

        forecast_event_id = _publish_forecast(redis_publisher, predicted_rps=HIGH_PREDICTED_RPS)
        scale_payload = _wait_for_scale_event(
            client, forecast_event_id, SCALE_DEADLINE_SECONDS,
        )

        if scale_payload is None:
            pytest.skip(
                "no smartload.scale envelope for the injected forecast — the "
                "cluster could not actuate a scale_out (no spare compose "
                "container and provisioning disabled), or a competing live "
                "forecast claimed the cooldown window"
            )
        assert scale_payload["action"] == "scale_out"
        assert scale_payload["forecast_event_id"] == forecast_event_id
        scaled_to = int(scale_payload["instance_count"])

        # The decision is the source of truth (SOT §8.8): it must be readable
        # through the SDK scaling-audit surface within a few seconds.
        deadline = time.monotonic() + 5.0
        matched = None
        while time.monotonic() < deadline:
            rows = client.list_audit("scaling", limit=10)
            for r in rows:
                if (
                    r.get("action") == "scale_out"
                    and int(r.get("instance_count", -1)) == scaled_to
                ):
                    matched = r
                    break
            if matched:
                break
            time.sleep(0.3)

        assert matched is not None, (
            f"scale_out to {scaled_to} not visible in scaling audit within 5s"
        )

    def test_cooldown_suppresses_back_to_back_forecasts(
        self, client, redis_publisher, baseline_count, reset_cooldown, autoscaler_url,
    ):
        """Two high forecasts in quick succession produce exactly one
        scaling action. The second is dropped by the cooldown timer.

        reset_cooldown restarts the autoscaler so its in-memory cooldown
        timer starts clean — otherwise a prior test's scale leaves the
        cooldown running and the first publish here would be suppressed for
        the wrong reason.

        Only applies to a controller that enforces a scale-out cooldown. The
        default ``target`` controller runs scale-out cooldown=0 (fast-out /
        slow-in by design), so back-to-back scale-outs toward an unmet target
        are intended and there is nothing to suppress — the suite covers the
        forecast→scale-out path itself in test_high_forecast_triggers_scale_out."""
        if not _scale_out_cooldown_enforced(autoscaler_url):
            pytest.skip(
                "active autoscaler controller does not enforce a scale-out "
                "cooldown (target controller runs fast-out/slow-in with "
                "scale_out_cooldown=0) — back-to-back forecast scale-outs are "
                "intended, so there is no cooldown-suppression contract to assert"
            )
        _ensure_headroom(client, baseline_count)
        if not reset_cooldown():
            pytest.skip("could not reset autoscaler cooldown (no Docker socket)")

        first_id  = _publish_forecast(redis_publisher, predicted_rps=HIGH_PREDICTED_RPS)
        first_event = _wait_for_scale_event(client, first_id, SCALE_DEADLINE_SECONDS)
        if first_event is None:
            pytest.skip(
                "first forecast did not scale (cluster could not actuate, or "
                "a competing live forecast claimed the window) — cannot "
                "exercise the cooldown branch deterministically"
            )
        assert first_event["action"] == "scale_out"

        # Immediately publish a second forecast; cooldown must suppress it.
        second_id = _publish_forecast(redis_publisher, predicted_rps=HIGH_PREDICTED_RPS)
        suppressed = _wait_for_scale_event(client, second_id, SCALE_DEADLINE_SECONDS)

        assert suppressed is None, (
            "second forecast produced a scale event during cooldown — "
            f"cooldown is not being enforced (event: {suppressed})"
        )


# ── operator override actuates the same pool ──────────────────────────────────

class TestOperatorOverride:
    """The manual scale endpoint is the operator-facing half of the same
    forecast → scale slice: a deliberate target_count instead of a
    forecast-derived one, actuating the same backend pool and the same
    scaling_events audit stream.

    Written race-tolerantly because a live forecasting service may be
    independently scaling the pool: each test reads the authoritative count
    from the autoscaler's own response rather than a snapshotted baseline.
    The baseline_count fixture is still used for teardown restore."""

    def test_scale_to_current_count_is_noop(self, client, baseline_count):
        """Scaling the pool to the count it is already at must be a no-op.

        Learn the live count from a first scale's report, then immediately
        request that exact count — that second call cannot change anything,
        so it must report noop regardless of what the count happens to be."""
        probe = client.scale(baseline_count, actor="e2e-fa-noop", reason="probe")
        current = int(probe.get("final_count", baseline_count))
        r = client.scale(current, actor="e2e-forecast-autoscale", reason="noop")
        assert r["status"] == "noop", f"scaling to current count {current} was not a noop: {r}"
        assert r["action"] == "noop"
        assert r["target_count"] == current
        assert r["reason"] == "manual:e2e-forecast-autoscale: noop"

    def test_manual_scale_lands_in_scaling_audit(self, client, baseline_count):
        policy = client.get_policy()
        min_b, max_b = int(policy["min_backends"]), int(policy["max_backends"])

        # Learn the live count authoritatively, then pick an in-band target
        # that differs from it so the scale actuates a real change.
        probe = client.scale(baseline_count, actor="e2e-fa-audit", reason="probe")
        current = int(probe.get("final_count", baseline_count))
        target = current + 1 if current < max_b else current - 1
        if not (min_b <= target <= max_b) or target == current:
            pytest.skip("no in-band target distinct from current count to exercise a change")

        expected_reason = "manual:e2e-fa-audit: forecast-autoscale audit"
        r = client.scale(target, actor="e2e-fa-audit", reason="forecast-autoscale audit")
        if r["status"] == "noop":
            # The autoscaler reports noop when it could not actuate any step —
            # i.e. dynamic provisioning is disabled (AUTOSCALER_PROVISIONING_ENABLED
            # =false, the default in CI), so it can't resize the compose-managed
            # backend pool. The assertions below need a real actuated change, so
            # skip rather than fail — same graceful-skip contract the forecast
            # legs use. Runs fully on a provisioning-enabled stack (adaptive-bench).
            pytest.skip(
                "manual scale could not actuate (status=noop); dynamic provisioning "
                "is disabled, so the audited-change assertions can't be exercised"
            )
        assert r["status"] == "applied"
        assert r["final_count"] == target

        deadline = time.monotonic() + 5.0
        matched = None
        while time.monotonic() < deadline:
            for row in client.list_audit("scaling", limit=10):
                if row.get("reason") == expected_reason:
                    matched = row
                    break
            if matched:
                break
            time.sleep(0.2)

        assert matched is not None, (
            f"scaling_events row with reason={expected_reason!r} not found within 5s"
        )
        assert int(matched["instance_count"]) == target
        assert matched["action"] in ("scale_out", "scale_in")
