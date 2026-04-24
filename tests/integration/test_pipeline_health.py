"""
tests/integration/test_pipeline_health.py
──────────────────────────────────────────
Integration pipeline health check — Phase 0 validation.

These 7 tests verify that the full SmartLoad pipeline is wired correctly
BEFORE any ML logic is implemented. All tests must pass before Phase 1 begins.

Architecture layers verified (from CommunicationsChannelsDiagram.png):
  1. HTTP traffic path       — load-balancer → test-backends
  2. Service reachability    — all 7 /health endpoints respond
  3. Redis connectivity      — all AI services connected to Redis
  4. TimescaleDB connectivity — all AI services connected to DB
  5. Control bus channels    — all smartload.* channels accessible
  6. Policy API              — GET /api/v1/policy returns valid fields
  7. DB schema               — required hypertables exist

Run:
    docker compose up -d
    pytest tests/integration/test_pipeline_health.py -v
    docker compose down
"""

import json

import psycopg2
import pytest
import redis as redis_lib
import requests

from tests.integration.conftest import (
    AI_SERVICES,
    REDIS_CHANNELS,
    REDIS_URL,
    REQUIRED_TABLES,
    SERVICE_URLS,
    TIMESCALEDB_DSN,
)


# ── Layer 1: HTTP Traffic Path ────────────────────────────────────────────────

class TestLoadBalancer:
    """Verify that NGINX routes traffic to the test-backend pool."""

    def test_load_balancer_routes_traffic(self, stack_ready):
        """GET / through the load balancer must return 200 with a backend response."""
        resp = requests.get(SERVICE_URLS["load-balancer"] + "/", timeout=10)
        assert resp.status_code == 200, (
            f"Load balancer returned {resp.status_code}. "
            "Is the test-backend running?"
        )
        body = resp.text
        assert "Hello from" in body or len(body) > 0, (
            "Unexpected empty response from load balancer"
        )

    def test_load_balancer_health_endpoint(self, stack_ready):
        """GET /health must return a healthy response from a backend."""
        resp = requests.get(SERVICE_URLS["load-balancer"] + "/health", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "healthy"


# ── Layer 2: Service Reachability ─────────────────────────────────────────────

class TestServiceReachability:
    """All 7 services must expose /health and return a non-500 status."""

    @pytest.mark.parametrize("service", list(SERVICE_URLS.keys()))
    def test_service_health_reachable(self, stack_ready, service):
        """Each service /health endpoint must respond without a server error."""
        url = SERVICE_URLS[service] + "/health"
        resp = requests.get(url, timeout=10)
        assert resp.status_code < 500, (
            f"{service} /health returned {resp.status_code}. "
            f"Body: {resp.text[:200]}"
        )
        data = resp.json()
        assert "status" in data, f"{service} /health missing 'status' field"
        # load-balancer proxies to Node.js test-backend which uses "server" key
        has_id = "service" in data or "server" in data
        assert has_id, f"{service} /health missing both 'service' and 'server' fields"


# ── Layer 3: Redis Connectivity ───────────────────────────────────────────────

class TestRedisConnectivity:
    """All AI services must report successful Redis connections via /health."""

    @pytest.mark.parametrize("service", AI_SERVICES)
    def test_service_connected_to_redis(self, stack_ready, service):
        """Each AI service /health must include 'redis': true."""
        url = SERVICE_URLS[service] + "/health"
        resp = requests.get(url, timeout=10)
        # Accept 200 (ok) or 207 (degraded but reachable)
        assert resp.status_code in (200, 207), (
            f"{service} returned unexpected status {resp.status_code}"
        )
        data = resp.json()
        assert "redis" in data, (
            f"{service} /health response missing 'redis' field. Got: {data}"
        )
        assert data["redis"] is True, (
            f"{service} is NOT connected to Redis. "
            f"Errors: {data.get('errors', [])}"
        )


# ── Layer 4: TimescaleDB Connectivity ─────────────────────────────────────────

class TestTimescaleDBConnectivity:
    """All AI services (except policy-manager) must report successful DB connections."""

    DB_SERVICES = [s for s in AI_SERVICES if s != "policy-manager"]

    @pytest.mark.parametrize("service", DB_SERVICES)
    def test_service_connected_to_timescaledb(self, stack_ready, service):
        """Each AI service /health must include 'timescaledb': true."""
        url = SERVICE_URLS[service] + "/health"
        resp = requests.get(url, timeout=10)
        assert resp.status_code in (200, 207)
        data = resp.json()
        assert "timescaledb" in data, (
            f"{service} /health response missing 'timescaledb' field. Got: {data}"
        )
        assert data["timescaledb"] is True, (
            f"{service} is NOT connected to TimescaleDB. "
            f"Errors: {data.get('errors', [])}"
        )


# ── Layer 5: Control Bus Channels ─────────────────────────────────────────────

class TestControlBus:
    """All smartload.* Redis Pub/Sub channels must be accessible."""

    def test_redis_direct_connection(self, stack_ready):
        """Must be able to connect to Redis directly from the test runner."""
        r = redis_lib.from_url(REDIS_URL, socket_connect_timeout=5)
        assert r.ping(), "Could not ping Redis"

    @pytest.mark.parametrize("channel", REDIS_CHANNELS)
    def test_redis_channel_publish(self, stack_ready, channel):
        """Must be able to publish a test message on each control bus channel."""
        r = redis_lib.from_url(REDIS_URL, socket_connect_timeout=5)
        test_message = json.dumps({"_test": True, "channel": channel})
        # publish returns the number of subscribers who received the message
        # (0 is fine at Phase 0 — no consumers yet, but publish must not raise)
        try:
            r.publish(channel, test_message)
        except Exception as exc:
            pytest.fail(
                f"Failed to publish to Redis channel '{channel}': {exc}"
            )


# ── Layer 6: Policy Manager API ───────────────────────────────────────────────

class TestPolicyManagerAPI:
    """The policy-manager must expose a working REST API with correct fields."""

    def test_get_policy_returns_200(self, stack_ready):
        """GET /api/v1/policy must return 200."""
        resp = requests.get(
            SERVICE_URLS["policy-manager"] + "/api/v1/policy", timeout=10
        )
        assert resp.status_code == 200, (
            f"Policy manager returned {resp.status_code}. Body: {resp.text[:200]}"
        )

    def test_get_policy_has_required_fields(self, stack_ready):
        """GET /api/v1/policy must include all required governance fields."""
        resp = requests.get(
            SERVICE_URLS["policy-manager"] + "/api/v1/policy", timeout=10
        )
        assert resp.status_code == 200
        policy = resp.json()
        required_fields = [
            "operating_mode",
            "safe_mode",
            "min_backends",
            "max_backends",
            "slo_p95_latency_ms",
        ]
        for field in required_fields:
            assert field in policy, (
                f"Policy response missing required field '{field}'. Got: {list(policy.keys())}"
            )

    def test_get_policy_safe_mode_is_bool(self, stack_ready):
        """safe_mode must be a boolean (not a string or int)."""
        resp = requests.get(
            SERVICE_URLS["policy-manager"] + "/api/v1/policy", timeout=10
        )
        policy = resp.json()
        assert isinstance(policy.get("safe_mode"), bool), (
            f"safe_mode must be bool, got {type(policy.get('safe_mode'))}"
        )


# ── Layer 7: TimescaleDB Schema ───────────────────────────────────────────────

class TestTimescaleDBSchema:
    """All required hypertables must exist in the database."""

    def test_timescaledb_direct_connection(self, stack_ready):
        """Must be able to connect to TimescaleDB directly from the test runner."""
        try:
            conn = psycopg2.connect(TIMESCALEDB_DSN, connect_timeout=10)
            conn.close()
        except Exception as exc:
            pytest.fail(f"Could not connect to TimescaleDB directly: {exc}")

    @pytest.mark.parametrize("table", REQUIRED_TABLES)
    def test_required_table_exists(self, stack_ready, table):
        """Each required hypertable must exist in the public schema."""
        conn = psycopg2.connect(TIMESCALEDB_DSN, connect_timeout=10)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = %s
                    """,
                    (table,),
                )
                row = cur.fetchone()
                assert row is not None, (
                    f"Required table '{table}' does not exist in TimescaleDB. "
                    "Check infrastructure/timescaledb/init.sql ran correctly."
                )
        finally:
            conn.close()

    def test_timescaledb_extension_enabled(self, stack_ready):
        """TimescaleDB extension must be installed in the database."""
        conn = psycopg2.connect(TIMESCALEDB_DSN, connect_timeout=10)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT extname FROM pg_extension WHERE extname = 'timescaledb';"
                )
                row = cur.fetchone()
                assert row is not None, "TimescaleDB extension is not installed."
        finally:
            conn.close()
