"""Pytest fixtures for the named-strategies e2e suite (#150).

Single upstream in play (policy-manager) because the slice is a translation
layer over POST /api/v1/policy. Uses the SmartLoad SDK so the suite exercises
the customer surface end-to-end.

The whole e2e session is gated by tests/e2e/conftest.py::_stack_ready, which
skips when policy-manager is unreachable — no extra skip wiring needed here.
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
def redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379")


@pytest.fixture(scope="function")
def client(policy_url, redis_url):
    """Per-test SmartLoadClient inside a context manager."""
    with SmartLoadClient(base_url=policy_url, redis_url=redis_url) as c:
        yield c


@pytest.fixture(scope="function")
def policy_restore(client):
    """Snapshot the policy before the test; restore on teardown so strategy
    flips don't leak into other suites. Mirrors the policy-management e2e
    fixture."""
    baseline = client.get_policy()
    yield baseline
    restore = {
        k: v for k, v in baseline.items()
        if k not in ("policy_version", "strategy_name")
    }
    try:
        client.set_policy(restore, actor="e2e-named-strategies-teardown")
    except Exception:
        pass
