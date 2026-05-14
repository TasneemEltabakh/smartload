"""Session-scoped readiness gate for the e2e test suite.

Skips the entire suite with a clear message if the docker-compose stack
is not reachable. Mirrors tests/integration/conftest.py::stack_ready but
self-contained so e2e can run independently from integration.
"""

from __future__ import annotations

import os
import time

import pytest

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


def _wait_for_url(url: str, timeout: int = 60, interval: float = 1.0) -> bool:
    if requests is None:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(url, timeout=3)
            if resp.status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


@pytest.fixture(scope="session", autouse=True)
def _stack_ready():
    """Block the session if policy-manager is not reachable."""
    target = os.environ.get("POLICY_URL", "http://localhost:8086") + "/health"
    timeout = int(os.environ.get("E2E_STACK_TIMEOUT", "60"))
    if not _wait_for_url(target, timeout=timeout):
        pytest.skip(
            f"e2e suite skipped: {target} not reachable within {timeout}s. "
            f"Start the stack with `docker compose up -d` and retry.",
            allow_module_level=True,
        )
    return True
