"""Pytest fixtures for the lb-sidecar e2e suite.

Requires a live docker-compose stack with LB_SIDECAR_RUNLOOP_ENABLED=true:
    LB_SIDECAR_RUNLOOP_ENABLED=true docker compose up -d
    pytest tests/e2e/lb-sidecar/ -v
"""

from __future__ import annotations

import os
import pytest
import httpx


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "e2e: end-to-end test requiring the live docker-compose stack",
    )


@pytest.fixture(scope="session")
def lb_sidecar_url() -> str:
    return os.environ.get("LB_SIDECAR_URL", "http://localhost:8087")


@pytest.fixture(scope="session")
def redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379")


@pytest.fixture(scope="session")
def http(lb_sidecar_url):
    with httpx.Client(base_url=lb_sidecar_url, timeout=10.0) as client:
        yield client
