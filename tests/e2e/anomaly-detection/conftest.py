"""Pytest fixtures for the anomaly-detection e2e suite.

Mirrors tests/e2e/manual-actions/conftest.py's SDK-client pattern, scoped to
the anomaly-detector + lb-sidecar surfaces this feature manifest covers.
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
def anomaly_detector_url() -> str:
    return os.environ.get(
        "SMARTLOAD_ANOMALY_DETECTOR_URL",
        os.environ.get("ANOMALY_DETECTOR_URL", "http://localhost:8082"),
    )


@pytest.fixture(scope="session")
def lb_sidecar_url() -> str:
    return os.environ.get("LB_SIDECAR_URL", "http://localhost:8087")


@pytest.fixture(scope="session")
def redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379")


@pytest.fixture(scope="function")
def client(anomaly_detector_url, redis_url):
    with SmartLoadClient(
        anomaly_detector_url=anomaly_detector_url,
        redis_url=redis_url,
    ) as c:
        yield c
