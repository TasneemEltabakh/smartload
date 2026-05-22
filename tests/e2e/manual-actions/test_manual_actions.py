"""
tests/e2e/manual-actions/test_manual_actions.py
────────────────────────────────────────────────
End-to-end suite for the manual-actions slice (#123). Uses the SDK
across three upstreams (policy-manager + autoscaler + anomaly-detector).

Requires a live docker-compose stack:
    docker compose up -d
    pytest tests/e2e/manual-actions/ -v
"""

from __future__ import annotations

import time

import httpx
import pytest

from smartload_client import SmartLoadClient, ValidationError

pytestmark = pytest.mark.e2e


def _other_in_band(current: int, min_b: int, max_b: int) -> int:
    """Pick an in-band target that's different from current."""
    if current < max_b:
        return current + 1
    return max(min_b, current - 1)


# ── scale: happy path + bounds ───────────────────────────────────────────────

class TestManualScale:

    def test_scale_to_in_band_target_returns_applied(
        self, client: SmartLoadClient, baseline_count,
    ):
        policy = client.get_policy()
        target = _other_in_band(
            baseline_count, int(policy["min_backends"]), int(policy["max_backends"]),
        )
        if target == baseline_count:
            pytest.skip("only one in-band target available; can't exercise change")
        r = client.scale(target, actor="e2e-scale", reason="happy path")
        assert r["status"] == "applied"
        assert r["target_count"] == target
        assert r["final_count"] == target
        assert r["previous_count"] == baseline_count
        assert r["action"] in ("scale_out", "scale_in")
        assert r["reason"] == "manual:e2e-scale: happy path"
        assert r["event_id"]

    def test_scale_to_same_count_is_noop(
        self, client: SmartLoadClient, baseline_count,
    ):
        r = client.scale(baseline_count, actor="e2e-scale", reason="noop test")
        assert r["status"] == "noop"
        assert r["action"] == "noop"
        assert r["steps_actuated"] == 0
        assert r["target_count"] == baseline_count

    def test_scale_above_max_raises_validation_error(
        self, client: SmartLoadClient, baseline_count,
    ):
        policy = client.get_policy()
        with pytest.raises(ValidationError) as exc:
            client.scale(int(policy["max_backends"]) + 99, actor="e2e-scale")
        assert exc.value.field == "target_count"

    def test_scale_below_min_raises_validation_error(
        self, client: SmartLoadClient, baseline_count,
    ):
        policy = client.get_policy()
        below = int(policy["min_backends"]) - 1
        if below < 0:
            below = -1
        with pytest.raises(ValidationError) as exc:
            client.scale(below, actor="e2e-scale")
        assert exc.value.field == "target_count"


# ── scale: audit round-trip ──────────────────────────────────────────────────

class TestManualScaleAudit:

    def test_scaling_event_appears_in_audit_within_5s(
        self, client: SmartLoadClient, baseline_count,
    ):
        policy = client.get_policy()
        target = _other_in_band(
            baseline_count, int(policy["min_backends"]), int(policy["max_backends"]),
        )
        if target == baseline_count:
            pytest.skip("only one in-band target available")
        expected_reason = "manual:e2e-audit-rt: round trip"
        client.scale(target, actor="e2e-audit-rt", reason="round trip")

        deadline = time.monotonic() + 5.0
        matched = None
        while time.monotonic() < deadline:
            rows = client.list_audit("scaling", limit=10)
            for r in rows:
                if r.get("reason") == expected_reason:
                    matched = r
                    break
            if matched:
                break
            time.sleep(0.2)

        assert matched is not None, (
            f"expected scaling_events row with reason={expected_reason!r} "
            f"within 5s"
        )
        assert matched["instance_count"] == target
        assert matched["action"] in ("scale_out", "scale_in")


# ── isolate: happy path + bounds ─────────────────────────────────────────────

class TestManualIsolate:

    def test_isolate_unhealthy_returns_applied(self, client: SmartLoadClient):
        r = client.isolate(
            "test-backend-3", "unhealthy",
            actor="e2e-isolate", reason="happy path",
        )
        assert r["status"] == "applied"
        assert r["backend_id"] == "test-backend-3"
        assert r["anomaly_status"] == "unhealthy"
        assert r["score"] == 1.0
        assert r["reason"] == "manual:e2e-isolate: happy path"
        assert r["event_id"]

    def test_isolate_healthy_uses_zero_score(self, client: SmartLoadClient):
        r = client.isolate(
            "test-backend-3", "healthy",
            actor="e2e-isolate", reason="recovery",
        )
        assert r["anomaly_status"] == "healthy"
        assert r["score"] == 0.0

    def test_isolate_invalid_status_raises_validation_error(
        self, client: SmartLoadClient,
    ):
        with pytest.raises(ValidationError) as exc:
            client.isolate("test-backend-3", "bogus")  # type: ignore[arg-type]
        assert exc.value.field == "status"

    def test_isolate_empty_backend_id_raises_validation_error(
        self, client: SmartLoadClient, anomaly_detector_url,
    ):
        """The SDK lets empty backend_id through; the server enforces the
        non-empty constraint with 400. We hit the endpoint directly to
        avoid the SDK pre-empting validation."""
        r = httpx.post(
            f"{anomaly_detector_url}/api/v1/isolate",
            json={"backend_id": "", "status": "unhealthy"},
            timeout=5.0,
        )
        assert r.status_code == 400
        assert r.json().get("field") == "backend_id"


# ── BFF proxy parity ─────────────────────────────────────────────────────────

class TestBFFProxyParity:
    """Both endpoints reach the same upstream whether called direct or via
    the operator-ui BFF. Smoke against future BFF refactors that drop a
    header or rewrite the body."""

    def test_bff_scale_proxies_to_autoscaler(self, client: SmartLoadClient, baseline_count):
        bff_url = "http://localhost:8090"
        r = httpx.post(
            f"{bff_url}/api/ui/scale",
            json={"target_count": baseline_count, "actor": "e2e-bff", "reason": "noop"},
            timeout=5.0,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "noop"
        assert body["reason"] == "manual:e2e-bff: noop"

    def test_bff_isolate_proxies_to_anomaly_detector(self, client: SmartLoadClient):
        bff_url = "http://localhost:8090"
        r = httpx.post(
            f"{bff_url}/api/ui/isolate",
            json={
                "backend_id": "test-backend-4",
                "status": "degraded",
                "actor": "e2e-bff",
                "reason": "bff proxy test",
            },
            timeout=5.0,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "applied"
        assert body["anomaly_status"] == "degraded"
        assert body["reason"] == "manual:e2e-bff: bff proxy test"
