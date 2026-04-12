"""
Smoke Test: Verify OpenTelemetry metrics are being emitted.

Run with pytest:
    cd tests/integration
    pip install -r requirements.txt
    pytest test_otel_smoke.py -v

Or directly:
    python test_otel_smoke.py

Requires: services running via docker-compose (cd infrastructure && docker-compose up -d)
"""

import time
import requests
import pytest

NGINX_URL   = "http://localhost:8080"
METRICS_URL = "http://localhost:9113/metrics"
HEALTH_URL  = "http://localhost:9113/health"

REQUIRED_METRICS = [
    "smartload_request_count_total",
    "smartload_request_latency_ms",
    "smartload_error_rate",
    "smartload_routing_backend_requests_total",
]

REQUIRED_LABELS = [
    "service_name",
    "instance_id",
    "node_id",
    "source",
    "environment",
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def generate_traffic():
    """
    Session-scoped fixture: runs once before any test.
    Sends 10 normal requests and 5 requests to a nonexistent path (→ 404)
    so that error metrics are populated before the metric assertions run.
    """
    for _ in range(10):
        try:
            requests.get(NGINX_URL, timeout=5)
        except requests.RequestException:
            pass
        time.sleep(0.1)

    for _ in range(5):
        try:
            requests.get(f"{NGINX_URL}/nonexistent", timeout=5)
        except requests.RequestException:
            pass
        time.sleep(0.1)

    # Give the exporter time to process the log lines
    time.sleep(3)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_exporter_health():
    """Metrics exporter /health returns the full documented response."""
    resp = requests.get(HEALTH_URL, timeout=5)
    assert resp.status_code == 200, f"Health check failed: {resp.status_code}"
    data = resp.json()
    assert data["status"] == "healthy"
    assert "service" in data,     f"Missing 'service' in health response: {data}"
    assert "instance_id" in data, f"Missing 'instance_id' in health response: {data}"


def test_nginx_health():
    """Nginx /health returns healthy."""
    resp = requests.get(f"{NGINX_URL}/health", timeout=5)
    assert resp.status_code == 200, f"Nginx health failed: {resp.status_code}"
    assert resp.json()["status"] == "healthy"


def test_metrics_endpoint_reachable():
    """The /metrics endpoint responds with 200."""
    resp = requests.get(METRICS_URL, timeout=5)
    assert resp.status_code == 200, f"Metrics endpoint failed: {resp.status_code}"
    assert resp.text.strip(), "Metrics response body is empty"


def test_required_metrics_present():
    """All required metric names are present in the /metrics output."""
    resp = requests.get(METRICS_URL, timeout=5)
    text = resp.text

    missing = [m for m in REQUIRED_METRICS if m not in text]
    assert not missing, (
        f"Missing metrics: {missing}\n\n"
        "Available smartload_ lines:\n" +
        "\n".join(l for l in text.splitlines() if l.startswith("smartload_"))
    )


def test_required_labels_present():
    """
    A smartload_request_count_total sample line contains all required labels.
    This validates the telemetry-v1 schema label contract.
    """
    resp = requests.get(METRICS_URL, timeout=5)
    text = resp.text

    sample_line = next(
        (l for l in text.splitlines()
         if "smartload_request_count_total{" in l and not l.startswith("#")),
        None,
    )
    assert sample_line is not None, (
        "Could not find a smartload_request_count_total sample line with labels"
    )

    missing_labels = [lb for lb in REQUIRED_LABELS if lb not in sample_line]
    assert not missing_labels, (
        f"Missing labels {missing_labels} in:\n  {sample_line}"
    )


def test_request_count_nonzero():
    """Request counter must be > 0 after traffic generation."""
    resp = requests.get(METRICS_URL, timeout=5)
    text = resp.text

    for line in text.splitlines():
        if "smartload_request_count_total{" in line and not line.startswith("#"):
            value = float(line.split()[-1])
            assert value > 0, f"Request count is 0: {line}"
            return

    pytest.fail("smartload_request_count_total sample line not found")


def test_error_metrics_recorded():
    """Error counter must be > 0 (5 × 404 requests were sent)."""
    resp = requests.get(METRICS_URL, timeout=5)
    text = resp.text

    for line in text.splitlines():
        if "smartload_error_count_total{" in line and not line.startswith("#"):
            value = float(line.split()[-1])
            assert value > 0, f"Error count is 0 despite sending 404 requests: {line}"
            return

    pytest.fail("smartload_error_count_total sample line not found")


def test_latency_histogram_has_samples():
    """Latency histogram _count must be > 0."""
    resp = requests.get(METRICS_URL, timeout=5)
    text = resp.text

    for line in text.splitlines():
        if "smartload_request_latency_ms_count{" in line and not line.startswith("#"):
            value = float(line.split()[-1])
            assert value > 0, f"Latency histogram count is 0: {line}"
            return

    pytest.fail("smartload_request_latency_ms_count line not found")


# ---------------------------------------------------------------------------
# Direct run (python test_otel_smoke.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))