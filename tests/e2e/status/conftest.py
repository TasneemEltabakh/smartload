"""Pytest fixtures for the consolidated-status e2e suite (slice #149 / OUI.9).

Hits the operator-UI BFF at `/api/v1/status` via the SmartLoad SDK so the
suite exercises the customer-facing surface end-to-end.
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
def operator_ui_url() -> str:
    return os.environ.get("OPERATOR_UI_URL", "http://localhost:8090")


@pytest.fixture(scope="session")
def policy_url() -> str:
    return os.environ.get("POLICY_URL", "http://localhost:8086")


@pytest.fixture
def client(operator_ui_url, policy_url) -> SmartLoadClient:
    c = SmartLoadClient(
        base_url=policy_url,
        operator_ui_url=operator_ui_url,
    )
    try:
        yield c
    finally:
        c.close()
