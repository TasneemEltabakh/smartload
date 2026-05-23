"""
tests/e2e/lb-sidecar/test_lb_sidecar.py
─────────────────────────────────────────
End-to-end suite for T2.1 (lb-sidecar — dynamic upstream rewriting).

Requires a live docker-compose stack with the sidecar run loop enabled:
    LB_SIDECAR_RUNLOOP_ENABLED=true docker compose up -d
    pytest tests/e2e/lb-sidecar/ -v

The suite exercises:
  1. /health endpoint — sidecar_ready, excluded_backends present.
  2. GET /api/v1/lb/state — valid upstream_weights + excluded_backends.
  3. POST /api/v1/lb/weights — operator weight override applies.
  4. Idempotency — POSTing the same weights twice returns ok=True both times.
  5. Empty-body POST returns 400.
  6. Routing recommendation flow — publish a mock active routing envelope on
     smartload.routing and verify weights change (integration path).
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

pytestmark = pytest.mark.e2e


# ── /health ───────────────────────────────────────────────────────────────────

class TestHealth:

    def test_health_returns_200_when_redis_ok(self, http):
        r = http.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["redis"] is True

    def test_health_includes_sidecar_fields_when_enabled(self, http):
        r = http.get("/health")
        body = r.json()
        # These fields appear only when LB_SIDECAR_RUNLOOP_ENABLED=true
        if "sidecar_ready" not in body:
            pytest.skip("LB_SIDECAR_RUNLOOP_ENABLED not enabled on this stack")
        assert isinstance(body["sidecar_ready"], bool)
        assert isinstance(body["excluded_backends"], list)
        assert "last_routing_age_seconds" in body


# ── /api/v1/lb/state ──────────────────────────────────────────────────────────

class TestLbState:

    def test_lb_state_returns_valid_shape(self, http):
        r = http.get("/api/v1/lb/state")
        if r.status_code == 503:
            pytest.skip("Run loop not enabled — /api/v1/lb/state requires it")
        assert r.status_code == 200
        body = r.json()
        assert "upstream_weights" in body
        assert "excluded_backends" in body
        assert isinstance(body["upstream_weights"], dict)
        assert isinstance(body["excluded_backends"], list)

    def test_lb_state_weights_are_positive_integers(self, http):
        r = http.get("/api/v1/lb/state")
        if r.status_code == 503:
            pytest.skip("Run loop not enabled")
        body = r.json()
        for backend, weight in body["upstream_weights"].items():
            assert isinstance(weight, int), f"{backend} weight is not int"
            assert weight >= 1, f"{backend} weight < 1"


# ── POST /api/v1/lb/weights ───────────────────────────────────────────────────

class TestSetWeights:

    _INITIAL_WEIGHTS = {
        "smartload-test-backend-1:8080": 80,
        "smartload-test-backend-2:8080": 60,
        "smartload-test-backend-3:8080": 40,
        "smartload-test-backend-4:8080": 40,
        "smartload-test-backend-5:8080": 40,
    }
    _EQUAL_WEIGHTS = {
        "smartload-test-backend-1:8080": 1,
        "smartload-test-backend-2:8080": 1,
        "smartload-test-backend-3:8080": 1,
        "smartload-test-backend-4:8080": 1,
        "smartload-test-backend-5:8080": 1,
    }

    def _state_available(self, http) -> bool:
        return http.get("/api/v1/lb/state").status_code == 200

    def test_set_weights_returns_ok(self, http):
        r = http.post("/api/v1/lb/weights", json=self._INITIAL_WEIGHTS)
        if r.status_code == 503:
            pytest.skip("Run loop not enabled")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert "applied_weights" in body

    def test_set_weights_reflected_in_state(self, http):
        if not self._state_available(http):
            pytest.skip("Run loop not enabled")
        http.post("/api/v1/lb/weights", json=self._INITIAL_WEIGHTS)
        state = http.get("/api/v1/lb/state").json()
        for backend, expected in self._INITIAL_WEIGHTS.items():
            if backend in state["upstream_weights"]:
                assert state["upstream_weights"][backend] == expected

    def test_set_weights_idempotent(self, http):
        if not self._state_available(http):
            pytest.skip("Run loop not enabled")
        r1 = http.post("/api/v1/lb/weights", json=self._INITIAL_WEIGHTS)
        r2 = http.post("/api/v1/lb/weights", json=self._INITIAL_WEIGHTS)
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r2.json()["ok"] is True

    def test_set_weights_empty_body_returns_400(self, http):
        r = http.post(
            "/api/v1/lb/weights",
            content=b"",
            headers={"Content-Type": "application/json"},
        )
        if r.status_code == 503:
            pytest.skip("Run loop not enabled")
        assert r.status_code == 400

    def test_restore_equal_weights(self, http):
        """Cleanup: leave all backends at equal weight=1."""
        if not self._state_available(http):
            pytest.skip("Run loop not enabled")
        r = http.post("/api/v1/lb/weights", json=self._EQUAL_WEIGHTS)
        assert r.status_code == 200


# ── Redis routing recommendation → weights (integration path) ─────────────────

class TestRoutingIntegration:

    def test_active_routing_envelope_updates_weights(self, http, redis_url):
        """Publish a mock active RoutingRecommendation to smartload.routing and
        verify the sidecar applies the weights within 10 seconds."""
        try:
            import redis as redis_lib
        except ImportError:
            pytest.skip("redis-py not installed — skipping routing integration test")

        state_r = http.get("/api/v1/lb/state")
        if state_r.status_code == 503:
            pytest.skip("Run loop not enabled")

        r_client = redis_lib.from_url(redis_url, decode_responses=True)
        envelope = json.dumps({
            "source": "e2e-test",
            "channel": "smartload.routing",
            "payload": {
                "mode": "active",
                "server_rankings": [
                    {"backend_id": "smartload-test-backend-1:8080", "score": 0.95},
                    {"backend_id": "smartload-test-backend-2:8080", "score": 0.50},
                    {"backend_id": "smartload-test-backend-3:8080", "score": 0.30},
                    {"backend_id": "smartload-test-backend-4:8080", "score": 0.30},
                    {"backend_id": "smartload-test-backend-5:8080", "score": 0.30},
                ],
                "policy_version": 0,
            },
        })
        r_client.publish("smartload.routing", envelope)

        # Poll for up to 10 s for weights to update
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            time.sleep(0.5)
            state = http.get("/api/v1/lb/state").json()
            weights = state.get("upstream_weights", {})
            b1 = weights.get("smartload-test-backend-1:8080")
            if b1 is not None and b1 > 1:
                assert b1 == 95, f"expected weight=95 for backend-1, got {b1}"
                return

        pytest.fail(
            "Weights were not updated within 10s after publishing active routing envelope"
        )
