"""Pytest fixtures for the manual-actions e2e suite.

Three upstreams in play (policy-manager + autoscaler + anomaly-detector)
because the slice touches all of them. Uses the SmartLoad SDK so the
suite exercises the customer surface end-to-end.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_SDK_ROOT = Path(__file__).resolve().parents[3] / "clients" / "python"
if str(_SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(_SDK_ROOT))

from smartload_client import SmartLoadClient  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "e2e: end-to-end test requiring the live docker-compose stack",
    )


@pytest.fixture(scope="session")
def policy_url() -> str:
    return os.environ.get("POLICY_URL", "http://localhost:8086")


@pytest.fixture(scope="session")
def autoscaler_url() -> str:
    return os.environ.get(
        "SMARTLOAD_AUTOSCALER_URL",
        os.environ.get("AUTOSCALER_URL", "http://localhost:8085"),
    )


@pytest.fixture(scope="session")
def anomaly_detector_url() -> str:
    return os.environ.get(
        "SMARTLOAD_ANOMALY_DETECTOR_URL",
        os.environ.get("ANOMALY_DETECTOR_URL", "http://localhost:8082"),
    )


@pytest.fixture(scope="session")
def redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379")


@pytest.fixture(scope="function")
def client(policy_url, autoscaler_url, anomaly_detector_url, redis_url):
    with SmartLoadClient(
        base_url=policy_url,
        autoscaler_url=autoscaler_url,
        anomaly_detector_url=anomaly_detector_url,
        redis_url=redis_url,
    ) as c:
        yield c


@pytest.fixture(scope="function")
def baseline_count(client):
    """Snapshot the starting backend count from the most recent scaling-audit
    row and restore the cluster to that count on teardown."""
    rows = client.list_audit("scaling", limit=1)
    if rows:
        start = int(rows[0]["instance_count"])
    else:
        # No history — assume max_backends as a safe upper bound and let the
        # test restore there.
        policy = client.get_policy()
        start = int(policy["max_backends"])
    yield start
    try:
        client.scale(start, actor="e2e-manual-teardown",
                     reason="restore baseline")
    except Exception:
        pass
