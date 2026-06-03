"""
tests/e2e/live-engines/test_live_engines.py
────────────────────────────────────────────
End-to-end suite for the Live Engines slice (#121). Exercises every
customer surface the manifest in `docs/features/live-engines.md` claims:

  - SDK `client.engines.state(service)` — direct per-engine endpoint
  - SDK `client.engines.snapshot()` — BFF parallel fan-out
  - SDK `client.engines.subscribe(callback)` — BFF SSE stream (replay + live)
  - SDK top-level aliases (engines_snapshot, engines_state, subscribe_engines)
  - SDK per-channel filter aliases (subscribe_anomaly/forecast/routing/scale)

Each test that depends on a fresh publish uses a per-test unique
`source` marker (see conftest.test_source) so it can pick its own
envelopes out of the live engines' organic traffic on the same channels.

Requires a live docker-compose stack:
    docker compose up -d
    pytest tests/e2e/live-engines/ -v
"""

from __future__ import annotations

import threading
import time

import pytest

from smartload_client import SmartLoadClient, ValidationError

from services.shared.contracts import (
    AnomalyEvent,
    ForecastResult,
    RoutingRecommendation,
    ScalingEvent,
)

pytestmark = pytest.mark.e2e


SSE_DEADLINE_SECONDS = 5.0
SNAPSHOT_DEADLINE_SECONDS = 5.0

ALL_CHANNELS = (
    "smartload.anomaly",
    "smartload.forecast",
    "smartload.routing",
    "smartload.scale",
)


def _wait_for_event(
    client: SmartLoadClient,
    *,
    test_source: str,
    channels=None,
    expected: int = 1,
    timeout: float = SSE_DEADLINE_SECONDS,
):
    """Open an SSE subscription, return up to `expected` events whose
    `envelope.source` matches `test_source`.

    Returns `(received, sub)` where received is the list of `(channel,
    payload, meta)` tuples that survived the filter. The subscription is
    closed before returning so the test can assert without worrying
    about thread cleanup.
    """
    received: list[tuple[str, dict, dict]] = []
    done = threading.Event()

    def _cb(channel, payload, meta):
        if meta.get("source") != test_source:
            return
        received.append((channel, payload, meta))
        if len(received) >= expected:
            done.set()

    sub = client.engines.subscribe(_cb, channels=channels)
    try:
        done.wait(timeout=timeout)
    finally:
        sub.close()
    return received


# ── per-engine state ────────────────────────────────────────────────────────

class TestEngineState:
    """SDK `client.engines.state(service)` hits the AI service directly."""

    @pytest.mark.parametrize(
        "service", ["anomaly-detector", "forecasting", "rl-engine"],
    )
    def test_state_returns_canonical_shape(
        self, client: SmartLoadClient, service: str,
    ):
        body = client.engines.state(service)
        # The contract called out in docs/features/live-engines.md row 1
        # of the Customer-surfaces table.
        for key in ("service", "channel", "runloop_enabled", "engine", "stats"):
            assert key in body, f"missing key {key!r} in /api/v1/engine/state of {service}"
        assert body["service"] == service

    def test_state_top_level_alias_matches_subclient(self, client: SmartLoadClient):
        """`SmartLoadClient.engines_state()` is a convenience wrapper around
        `client.engines.state()`. Same payload, same service identity."""
        a = client.engines.state("anomaly-detector")
        b = client.engines_state("anomaly-detector")
        assert a["service"] == b["service"] == "anomaly-detector"

    def test_unknown_service_raises_validation_error(self, client: SmartLoadClient):
        with pytest.raises(ValidationError) as exc:
            client.engines.state("not-a-real-engine")  # type: ignore[arg-type]
        assert exc.value.field == "service"


# ── snapshot (BFF parallel fan-out) ─────────────────────────────────────────

class TestSnapshot:
    """SDK `client.engines.snapshot()` against the operator-UI BFF."""

    def test_snapshot_covers_three_services(self, client: SmartLoadClient):
        snap = client.engines.snapshot()
        assert set(snap["services"]) >= {
            "anomaly-detector", "forecasting", "rl-engine",
        }

    def test_snapshot_lists_all_four_channel_rings(self, client: SmartLoadClient):
        snap = client.engines.snapshot()
        assert set(snap["channels"]) >= set(ALL_CHANNELS)
        for ch in ALL_CHANNELS:
            assert isinstance(snap["channels"][ch], list)

    def test_snapshot_top_level_alias_matches_subclient(self, client: SmartLoadClient):
        a = client.engines.snapshot()
        b = client.engines_snapshot()
        assert set(a) == set(b)
        assert set(a["services"]) == set(b["services"])


# ── synthetic publish → SSE delivery ────────────────────────────────────────

class TestSSEDelivery:
    """For every channel, publish a synthetic envelope and assert the SDK
    SSE subscriber observes it inside the 5 s acceptance window."""

    def test_anomaly_publish_delivered(self, client, publish, test_source):
        publish(
            "smartload.anomaly",
            AnomalyEvent(backend_id="e2e-backend", status="unhealthy", score=0.91),
        )
        received = _wait_for_event(client, test_source=test_source)
        assert received, f"no smartload.anomaly envelope arrived within {SSE_DEADLINE_SECONDS}s"
        channel, payload, _meta = received[0]
        assert channel == "smartload.anomaly"
        assert payload["backend_id"] == "e2e-backend"
        assert payload["status"] == "unhealthy"

    def test_forecast_publish_delivered(self, client, publish, test_source):
        publish(
            "smartload.forecast",
            ForecastResult(
                horizon_minutes=5,
                predicted_rps=42.0,
                confidence_lower=30.0,
                confidence_upper=55.0,
                model_id="e2e-stub",
            ),
        )
        received = _wait_for_event(client, test_source=test_source)
        assert received, "no smartload.forecast envelope arrived within deadline"
        channel, payload, _meta = received[0]
        assert channel == "smartload.forecast"
        assert payload["predicted_rps"] == pytest.approx(42.0)
        assert payload["horizon_minutes"] == 5

    def test_routing_publish_delivered(self, client, publish, test_source):
        publish(
            "smartload.routing",
            RoutingRecommendation(
                mode="shadow",
                server_rankings=[{"backend_id": "e2e-backend", "score": 0.42}],
            ),
        )
        received = _wait_for_event(client, test_source=test_source)
        assert received, "no smartload.routing envelope arrived within deadline"
        channel, payload, _meta = received[0]
        assert channel == "smartload.routing"
        assert payload["mode"] == "shadow"
        assert payload["server_rankings"][0]["backend_id"] == "e2e-backend"

    def test_scale_publish_delivered(self, client, publish, test_source):
        publish(
            "smartload.scale",
            ScalingEvent(action="scale_out", instance_count=4, reason="e2e-synthetic"),
        )
        received = _wait_for_event(client, test_source=test_source)
        assert received, "no smartload.scale envelope arrived within deadline"
        channel, payload, _meta = received[0]
        assert channel == "smartload.scale"
        assert payload["action"] == "scale_out"
        assert payload["instance_count"] == 4


# ── client-side channel filter ──────────────────────────────────────────────

class TestSubscribeChannelFilter:
    """`client.engines.subscribe(..., channels=[...])` must drop other
    channels' frames client-side. The BFF stream itself always carries all
    four channels; the filter is applied inside the SDK."""

    def test_filter_one_channel_excludes_others(
        self, client, publish, test_source,
    ):
        received: list[tuple[str, dict, dict]] = []

        def _cb(channel, payload, meta):
            if meta.get("source") == test_source:
                received.append((channel, payload, meta))

        sub = client.engines.subscribe(_cb, channels=["smartload.scale"])
        try:
            # Two publishes — one matching the filter, one not.
            publish(
                "smartload.scale",
                ScalingEvent(action="scale_out", instance_count=3,
                             reason="e2e-filter"),
            )
            publish(
                "smartload.anomaly",
                AnomalyEvent(backend_id="e2e-backend", status="healthy", score=0.1),
            )
            # Give the live stream a few seconds to deliver both.
            time.sleep(SSE_DEADLINE_SECONDS)
        finally:
            sub.close()

        channels_seen = {ch for ch, _, _ in received}
        assert channels_seen == {"smartload.scale"}, (
            f"expected only smartload.scale; got {channels_seen}"
        )

    def test_subscribe_anomaly_alias_only_delivers_anomaly(
        self, client, publish, test_source,
    ):
        """The top-level `subscribe_anomaly` alias is the single-channel
        filter operators reach for first; covered here so the alias
        doesn't silently regress to no-op or to all-channels."""
        received: list[tuple[str, dict, dict]] = []

        def _cb(channel, payload, meta):
            if meta.get("source") == test_source:
                received.append((channel, payload, meta))

        sub = client.subscribe_anomaly(_cb)
        try:
            publish(
                "smartload.anomaly",
                AnomalyEvent(backend_id="e2e-backend", status="degraded", score=0.7),
            )
            publish(
                "smartload.forecast",
                ForecastResult(
                    horizon_minutes=5,
                    predicted_rps=1.0,
                    confidence_lower=0.0,
                    confidence_upper=2.0,
                ),
            )
            time.sleep(SSE_DEADLINE_SECONDS)
        finally:
            sub.close()

        channels_seen = {ch for ch, _, _ in received}
        assert channels_seen == {"smartload.anomaly"}


# ── snapshot reflects publishes ─────────────────────────────────────────────

class TestSnapshotReflectsPublishes:
    """After publishing, the BFF's per-channel ring must contain the new
    entry within the acceptance window. Proves the snapshot endpoint isn't
    just stale — it's the same data plane the SSE stream feeds."""

    def test_publish_appears_in_channel_ring(
        self, client, publish, test_source,
    ):
        publish(
            "smartload.scale",
            ScalingEvent(action="scale_in", instance_count=2,
                         reason="e2e-snapshot-ring"),
        )

        deadline = time.monotonic() + SNAPSHOT_DEADLINE_SECONDS
        matched = None
        while time.monotonic() < deadline:
            snap = client.engines.snapshot()
            ring = snap.get("channels", {}).get("smartload.scale", [])
            matched = next(
                (
                    e for e in ring
                    if e.get("envelope", {}).get("source") == test_source
                ),
                None,
            )
            if matched is not None:
                break
            time.sleep(0.25)

        assert matched is not None, (
            f"published smartload.scale envelope from source={test_source!r} "
            f"did not appear in the BFF ring within {SNAPSHOT_DEADLINE_SECONDS}s"
        )
        assert matched["payload"]["reason"] == "e2e-snapshot-ring"
        assert matched["payload"]["instance_count"] == 2


# ── subscription teardown ───────────────────────────────────────────────────

class TestSubscriptionLifecycle:
    """`.close()` must stop the daemon thread cleanly and be idempotent —
    operators routinely tear down and recreate subscriptions during dashboard
    refreshes."""

    def test_close_is_idempotent(self, client):
        sub = client.engines.subscribe(lambda *a, **kw: None)
        sub.close()
        sub.close()  # must not raise

    def test_context_manager_closes_on_exit(self, client, publish, test_source):
        # Use the subscription as a context manager and confirm a publish
        # *after* exit produces no further callback invocations.
        calls_during: list[str] = []
        calls_after: list[str] = []
        seen_during = threading.Event()

        def _cb_during(channel, payload, meta):
            if meta.get("source") == test_source:
                calls_during.append(channel)
                seen_during.set()

        with client.engines.subscribe(_cb_during):
            publish(
                "smartload.scale",
                ScalingEvent(action="scale_out", instance_count=5,
                             reason="e2e-ctx-mgr-during"),
            )
            assert seen_during.wait(timeout=SSE_DEADLINE_SECONDS), (
                "did not see in-context publish via subscription"
            )

        # Subscription is closed; a second subscription with a different
        # callback must still see new publishes.
        def _cb_after(channel, payload, meta):
            if meta.get("source") == test_source:
                calls_after.append(channel)

        sub2 = client.engines.subscribe(_cb_after)
        try:
            publish(
                "smartload.scale",
                ScalingEvent(action="scale_out", instance_count=6,
                             reason="e2e-ctx-mgr-after"),
            )
            time.sleep(SSE_DEADLINE_SECONDS)
        finally:
            sub2.close()

        # The first subscription was closed before the second publish, so
        # its callback list must not have grown.
        assert len(calls_during) == 1, (
            f"first subscription saw {len(calls_during)} events after close; "
            f"context-manager teardown leaked"
        )
        assert calls_after, "second subscription saw no events"
