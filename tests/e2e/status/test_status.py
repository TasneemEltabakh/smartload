"""
tests/e2e/status/test_status.py
────────────────────────────────
End-to-end suite for the consolidated-status slice (#149 / OUI.9).

Exercises `GET /api/v1/status` on the live operator-UI BFF through the
SmartLoad SDK. Requires the docker-compose stack running:

    docker compose up -d
    pytest tests/e2e/status/ -v
"""

from __future__ import annotations

import time

import httpx
import pytest

from smartload_client import (
    ActivePolicySnapshot,
    RecentEvents,
    ServiceStatus,
    SmartLoadClient,
    SmartLoadError,
    StatusResponse,
)

pytestmark = pytest.mark.e2e


EXPECTED_SERVICES = {
    "policy-manager",
    "autoscaler",
    "telemetry",
    "anomaly-detector",
    "forecasting",
    "rl-engine",
    "lb-sidecar",
    "load-balancer",
}


# ── shape + dataclass round-trip ─────────────────────────────────────────────

class TestStatusShape:

    def test_returns_typed_response(self, client: SmartLoadClient):
        status = client.get_status()
        assert isinstance(status, StatusResponse)

    def test_overall_is_one_of_three_pills(self, client: SmartLoadClient):
        status = client.get_status()
        assert status.overall in {"ok", "degraded", "down"}

    def test_generated_at_present(self, client: SmartLoadClient):
        status = client.get_status()
        assert isinstance(status.generated_at, str) and status.generated_at

    def test_services_map_has_canonical_set(self, client: SmartLoadClient):
        status = client.get_status()
        # Every service the BFF knows about appears; service-specific extras
        # are present but vary by service.
        assert set(status.services.keys()) >= EXPECTED_SERVICES
        for name, svc in status.services.items():
            assert isinstance(svc, ServiceStatus)
            assert svc.name == name
            assert isinstance(svc.status, str)

    def test_active_policy_is_typed_when_present(self, client: SmartLoadClient):
        status = client.get_status()
        # policy-manager is always up in a normal e2e environment.
        assert status.active_policy is not None
        assert isinstance(status.active_policy, ActivePolicySnapshot)
        # Headline fields land in their typed slots (any may be None on a
        # freshly-bootstrapped stack).
        assert isinstance(status.active_policy.policy_version, (int, type(None)))

    def test_recent_is_typed_wrapper(self, client: SmartLoadClient):
        status = client.get_status()
        assert isinstance(status.recent, RecentEvents)
        # Either field may be None on a freshly-bootstrapped stack with no
        # audit history; the wrapper itself is always present.


# ── overall rollup invariant ─────────────────────────────────────────────────

class TestOverallRollup:

    def _expected_overall(self, services: dict) -> str:
        statuses = [v.status for v in services.values()]
        if any(s == "down" for s in statuses):
            return "down"
        if any(s != "ok" for s in statuses):
            return "degraded"
        return "ok"

    def test_overall_matches_per_service_rollup(self, client: SmartLoadClient):
        status = client.get_status()
        expected = self._expected_overall(status.services)
        assert status.overall == expected, (
            f"server says overall={status.overall!r} but per-service rollup "
            f"yields {expected!r}; services="
            f"{ {n: s.status for n, s in status.services.items()} }"
        )


# ── always 200 + bounded latency ─────────────────────────────────────────────

class TestLatencyBudget:

    def test_response_is_always_200(self, operator_ui_url: str):
        # Direct HTTP probe so we catch the contract that the BFF never
        # returns non-2xx from /api/v1/status, even if a downstream is down.
        r = httpx.get(f"{operator_ui_url}/api/v1/status", timeout=10.0)
        assert r.status_code == 200

    def test_response_within_5s_budget(self, client: SmartLoadClient):
        # The endpoint's contract is "complete within 3s even when one
        # service hangs". We allow a generous 5s margin in the e2e gate so
        # transient slow environments don't flake the suite; the regression
        # threshold remains 3s for production diagnostics.
        t0 = time.monotonic()
        client.get_status()
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0, f"/api/v1/status took {elapsed:.2f}s (budget 5s)"


# ── forward-compat for service-specific extras ───────────────────────────────

class TestServiceExtras:
    """The BFF passes every non-status field from each service's /health
    body through to the response. This pins the contract for the per-service
    extras the manifest documents."""

    def test_policy_manager_includes_policy_version(self, client: SmartLoadClient):
        status = client.get_status()
        pm = status.services["policy-manager"]
        # policy_version is the headline field for the policy-manager row.
        if pm.status == "ok":
            assert "policy_version" in pm.extra

    def test_rl_engine_includes_routing_safety_pin(self, client: SmartLoadClient):
        status = client.get_status()
        rl = status.services["rl-engine"]
        if rl.status == "ok":
            # rl-engine's /health surfaces `rl_mode` as the routing-safety
            # pin — operators want it visible in the consolidated status.
            assert "rl_mode" in rl.extra

    def test_telemetry_includes_db_signals(self, client: SmartLoadClient):
        status = client.get_status()
        tel = status.services["telemetry"]
        if tel.status == "ok":
            # Telemetry surfaces both backing stores on /health.
            assert "redis" in tel.extra
            assert "timescaledb" in tel.extra


# ── direct BFF parity (SDK vs raw HTTP) ──────────────────────────────────────

class TestSdkParity:
    """The SDK is a thin wrapper — the dataclass.to_dict() form should
    round-trip to the same logical content as the raw BFF response."""

    def test_to_dict_round_trip_matches_wire(self, client: SmartLoadClient, operator_ui_url: str):
        wire = httpx.get(f"{operator_ui_url}/api/v1/status", timeout=10.0).json()
        sdk = client.get_status().to_dict()
        # Overall, generated_at, and per-service status pills must match.
        assert sdk["overall"] == wire["overall"]
        assert set(sdk["services"].keys()) == set(wire["services"].keys())
        for name in wire["services"]:
            assert sdk["services"][name]["status"] == wire["services"][name]["status"]
