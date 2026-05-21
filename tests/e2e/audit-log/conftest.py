"""Pytest fixtures for the audit-log e2e suite.

Uses the SmartLoad SDK exclusively so the suite exercises the customer-
facing surface end-to-end across two upstreams: policy-manager (for
policy audit) and autoscaler (for scaling audit).

Mirrors tests/e2e/policy-management/conftest.py — the only new bit is
the autoscaler_url fixture, since the scaling-audit endpoint lives on
a different service than the policy-manager base_url.
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
def redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379")


@pytest.fixture(scope="function")
def client(policy_url, autoscaler_url, redis_url):
    """Per-test SmartLoadClient with both upstreams wired."""
    with SmartLoadClient(
        base_url=policy_url,
        autoscaler_url=autoscaler_url,
        redis_url=redis_url,
    ) as c:
        yield c


@pytest.fixture(scope="function")
def policy_restore(client):
    """Snapshot the policy before the test; restore it on teardown so
    audit tests that mutate state don't leave drift behind."""
    baseline = client.get_policy()
    yield baseline
    restore = {k: v for k, v in baseline.items() if k != "policy_version"}
    try:
        client.set_policy(restore, actor="e2e-audit-teardown")
    except Exception:
        pass
