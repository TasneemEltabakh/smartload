"""
tests/e2e/anomaly-detection/test_anomaly_detection.py
───────────────────────────────────────────────────────
End-to-end suite for the anomaly-detection slice
(docs/features/anomaly-detection.md). Exercises every customer surface the
manifest claims:

  - GET /health — engine_type / engine_ready / engine_requested fields
  - SDK client.engines.state("anomaly-detector") — policy_snapshot exposes
    the Phase-1 stability-gate fields (flip_confirmation_cycles,
    min_sample_count, anomaly_response)
  - SDK client.isolate(backend_id, status, ...) — manual override round trip
  - smartload.anomaly envelope delivery via the BFF SSE stream
    (client.engines.subscribe)
  - lb-sidecar /api/v1/lb/state — excluded_backends reflects an isolated
    backend (skipped if the sidecar run loop is disabled)

Requires a live docker-compose stack:
    docker compose up -d
    pytest tests/e2e/anomaly-detection/ -v
"""

from __future__ import annotations

import threading

import httpx
import pytest

from smartload_client import SmartLoadClient

pytestmark = pytest.mark.e2e

SSE_DEADLINE_SECONDS = 30.0


def _wait_for_isolate_event(client: SmartLoadClient, backend_id: str, status: str, timeout: float):
    """Subscribe to smartload.anomaly and block until an envelope matching
    backend_id + status arrives, or timeout. Returns (channel, payload, meta)
    or None.

    isolate-triggered envelopes always carry envelope.source ==
    "anomaly-detector" (not a test-unique marker), so filtering is done on
    the payload's backend_id/status instead.
    """
    received: list[tuple[str, dict, dict]] = []
    done = threading.Event()

    def _cb(channel, payload, meta):
        if channel != "smartload.anomaly":
            return
        if payload.get("backend_id") == backend_id and payload.get("status") == status:
            received.append((channel, payload, meta))
            done.set()

    sub = client.engines.subscribe(_cb, channels=["smartload.anomaly"])
    try:
        done.wait(timeout=timeout)
    finally:
        sub.close()
    return received[0] if received else None


# ── /health ───────────────────────────────────────────────────────────────────

class TestHealth:

    def test_health_reports_engine_fields(self, client: SmartLoadClient, anomaly_detector_url):
        r = httpx.get(f"{anomaly_detector_url}/health", timeout=10.0)
        assert r.status_code in (200, 503)
        body = r.json()
        for key in ("status", "service", "redis", "timescaledb"):
            assert key in body, f"missing key {key!r} in /health: {body}"
        if "engine_type" in body:
            assert "engine_ready" in body
            assert "engine_requested" in body
            assert "last_inference_age_seconds" in body


# ── /api/v1/engine/state ──────────────────────────────────────────────────────

class TestEngineState:

    def test_policy_snapshot_exposes_stability_gate_fields(self, client: SmartLoadClient):
        body = client.engines.state("anomaly-detector")
        assert body["service"] == "anomaly-detector"
        policy = body.get("policy_snapshot", {})
        for key in ("flip_confirmation_cycles", "min_sample_count", "anomaly_response"):
            assert key in policy, f"policy_snapshot missing {key!r}: {policy}"
        assert policy["flip_confirmation_cycles"] >= 1


# ── isolate: manual override round trip ──────────────────────────────────────

class TestIsolateRoundTrip:
    BACKEND_ID = "smartload-test-backend-2:8080"

    def test_isolate_unhealthy_then_healthy(self, client: SmartLoadClient):
        r1 = client.isolate(
            self.BACKEND_ID, "unhealthy",
            actor="e2e-anomaly-detection", reason="isolate round trip",
        )
        assert r1["status"] == "applied"
        assert r1["backend_id"] == self.BACKEND_ID
        assert r1["anomaly_status"] == "unhealthy"
        assert r1["score"] == 1.0
        assert r1["event_id"]

        r2 = client.isolate(
            self.BACKEND_ID, "healthy",
            actor="e2e-anomaly-detection", reason="isolate recovery",
        )
        assert r2["status"] == "applied"
        assert r2["anomaly_status"] == "healthy"
        assert r2["score"] == 0.0
        assert r2["event_id"]


# ── smartload.anomaly envelope delivery ───────────────────────────────────────

class TestAnomalyEventDelivery:
    BACKEND_ID = "smartload-test-backend-1:8080"

    def test_isolate_unhealthy_emits_anomaly_event(self, client: SmartLoadClient):
        received: list[tuple[str, dict, dict]] = []
        done = threading.Event()

        def _cb(channel, payload, meta):
            if channel != "smartload.anomaly":
                return
            if payload.get("backend_id") == self.BACKEND_ID and payload.get("status") == "unhealthy":
                received.append((channel, payload, meta))
                done.set()

        sub = client.engines.subscribe(_cb, channels=["smartload.anomaly"])
        try:
            r = client.isolate(
                self.BACKEND_ID, "unhealthy",
                actor="e2e-anomaly-detection", reason="event delivery",
            )
            assert r["status"] == "applied"
            assert done.wait(timeout=SSE_DEADLINE_SECONDS), (
                f"no smartload.anomaly envelope for {self.BACKEND_ID}/unhealthy "
                f"within {SSE_DEADLINE_SECONDS}s"
            )
        finally:
            sub.close()
            client.isolate(
                self.BACKEND_ID, "healthy",
                actor="e2e-anomaly-detection", reason="event delivery cleanup",
            )

        channel, payload, meta = received[0]
        assert channel == "smartload.anomaly"
        assert payload["score"] == 1.0
        assert payload.get("model_version", "").startswith("manual:")
        assert meta.get("source") == "anomaly-detector"


# ── lb-sidecar exclusion ───────────────────────────────────────────────────────

class TestLbSidecarExclusion:
    BACKEND_ID = "smartload-test-backend-5:8080"

    def test_isolated_backend_is_excluded_from_lb_state(self, client: SmartLoadClient, lb_sidecar_url):
        lb_http = httpx.Client(base_url=lb_sidecar_url.rstrip("/"), timeout=10.0)
        state_r = lb_http.get("/api/v1/lb/state")
        if state_r.status_code == 503:
            pytest.skip("lb-sidecar run loop not enabled — /api/v1/lb/state requires it")
        assert state_r.status_code == 200

        try:
            r = client.isolate(
                self.BACKEND_ID, "unhealthy",
                actor="e2e-anomaly-detection", reason="lb exclusion check",
            )
            assert r["status"] == "applied"

            payload = _wait_for_isolate_event(client, self.BACKEND_ID, "unhealthy", SSE_DEADLINE_SECONDS)
            assert payload is not None, (
                f"no smartload.anomaly envelope for {self.BACKEND_ID}/unhealthy "
                f"within {SSE_DEADLINE_SECONDS}s"
            )

            state = lb_http.get("/api/v1/lb/state").json()
            assert self.BACKEND_ID in state.get("excluded_backends", []), (
                f"expected {self.BACKEND_ID} in excluded_backends after isolate: {state}"
            )
        finally:
            r = client.isolate(
                self.BACKEND_ID, "healthy",
                actor="e2e-anomaly-detection", reason="lb exclusion cleanup",
            )
            assert r["status"] == "applied"
            _wait_for_isolate_event(client, self.BACKEND_ID, "healthy", SSE_DEADLINE_SECONDS)

        state = lb_http.get("/api/v1/lb/state").json()
        assert self.BACKEND_ID not in state.get("excluded_backends", []), (
            f"expected {self.BACKEND_ID} re-included after recovery: {state}"
        )
