"""
tests/integration/conftest.py
──────────────────────────────
Shared pytest fixtures for SmartLoad integration tests.

Requires the full docker-compose stack to be running before pytest is invoked:
    docker compose up -d
    pytest tests/integration/ -v
    docker compose down
"""

import time

import pytest
import requests

# ── service registry ──────────────────────────────────────────────────────────
# Maps service names to their base URLs as exposed by docker-compose.

SERVICE_URLS = {
    "load-balancer":    "http://localhost:8080",
    "telemetry":        "http://localhost:8081",
    "anomaly-detector": "http://localhost:8082",
    "forecasting":      "http://localhost:8083",
    "rl-engine":        "http://localhost:8084",
    "autoscaler":       "http://localhost:8085",
    "policy-manager":   "http://localhost:8086",
}

# AI services that must report redis + timescaledb connectivity
AI_SERVICES = [
    "telemetry",
    "anomaly-detector",
    "forecasting",
    "rl-engine",
    "autoscaler",
]

# TimescaleDB connection params for direct DB checks
TIMESCALEDB_DSN = "host=localhost port=5432 dbname=smartloaddb user=postgres password=changeme"

# Redis connection params for direct channel checks
REDIS_URL = "redis://localhost:6379"

# Channels that must exist on the control bus
REDIS_CHANNELS = [
    "smartload.anomaly",
    "smartload.forecast",
    "smartload.routing",
    "smartload.scale",
    "smartload.policy",
]

# Tables that must exist in TimescaleDB
REQUIRED_TABLES = ["metrics", "backend_health", "scaling_events", "policy_changes"]


# ── helpers ───────────────────────────────────────────────────────────────────

def wait_for_url(url: str, timeout: int = 60, interval: float = 2.0):
    """
    Poll GET url until a non-500 response or timeout.
    Raises TimeoutError if the service does not become reachable in time.
    """
    deadline = time.time() + timeout
    last_exc = None
    while time.time() < deadline:
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code < 500:
                return resp
        except Exception as exc:
            last_exc = exc
        time.sleep(interval)
    raise TimeoutError(
        f"Service not reachable at {url} after {timeout}s. Last error: {last_exc}"
    )


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def services():
    """Returns the service URL map. Tests use this to build endpoint URLs."""
    return SERVICE_URLS


@pytest.fixture(scope="session")
def stack_ready(services):
    """
    Session-scoped fixture: wait for all services to respond before any test runs.
    Fails fast with a clear message if any service cannot be reached.
    """
    for name, base_url in services.items():
        health_url = f"{base_url}/health" if name != "load-balancer" else f"{base_url}/health"
        try:
            wait_for_url(health_url, timeout=90)
        except TimeoutError as exc:
            pytest.fail(f"[stack_ready] {name} did not become reachable: {exc}")
    return services
