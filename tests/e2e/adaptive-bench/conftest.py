"""Pytest fixtures for the adaptive-bench Round 2 e2e suite (#156)."""

from __future__ import annotations

import os

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "e2e: end-to-end test requiring the live docker-compose stack",
    )


@pytest.fixture(scope="session")
def status_url() -> str:
    return os.environ.get("OPERATOR_UI_URL", "http://localhost:8090") + "/api/v1/status"
