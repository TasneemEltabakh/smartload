"""Pytest fixtures for the policy-management e2e suite.

The suite uses the SmartLoad SDK exclusively (no raw HTTP / Redis) so it
exercises the customer-facing surface end-to-end.

Reuses connection constants from tests/integration/conftest.py where possible.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the SDK importable when running from the repo without `pip install -e`.
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
def redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379")


@pytest.fixture(scope="function")
def client(policy_url, redis_url):
    """Per-test SmartLoadClient inside a context manager."""
    with SmartLoadClient(base_url=policy_url, redis_url=redis_url) as c:
        yield c


@pytest.fixture(scope="function")
def policy_restore(client):
    """Snapshot the policy before the test; restore on teardown.

    Mirrors tests/integration/test_policy_manager.py::policy_backup so e2e
    tests can run interleaved with integration tests without leaving state
    behind.
    """
    baseline = client.get_policy()
    yield baseline
    # Restore — drop policy_version so the service recomputes it.
    restore = {k: v for k, v in baseline.items() if k != "policy_version"}
    try:
        client.set_policy(restore, actor="e2e-teardown")
    except Exception:
        pass
