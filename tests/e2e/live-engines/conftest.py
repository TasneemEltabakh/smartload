"""Pytest fixtures for the live-engines e2e suite (#121).

Exercises the slice through every customer surface: the SDK
(`engines.snapshot/state/subscribe` + the top-level aliases), the
operator-UI BFF (`/api/ui/engines/{snapshot,stream}`), and the three AI
services' `GET /api/v1/engine/state` endpoints. The publisher path uses
the canonical `publish_envelope` from `services.shared.contracts` so the
test is wire-compatible with how the AI services emit.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
import redis as redis_lib

_SDK_ROOT = Path(__file__).resolve().parents[3] / "clients" / "python"
if str(_SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(_SDK_ROOT))

from smartload_client import SmartLoadClient  # noqa: E402

from services.shared.contracts import publish_envelope  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "e2e: end-to-end test requiring the live docker-compose stack",
    )


@pytest.fixture(scope="session")
def policy_url() -> str:
    return os.environ.get("POLICY_URL", "http://localhost:8086")


@pytest.fixture(scope="session")
def operator_ui_url() -> str:
    return os.environ.get(
        "SMARTLOAD_OPERATOR_UI_URL",
        os.environ.get("OPERATOR_UI_URL", "http://localhost:8090"),
    )


@pytest.fixture(scope="session")
def redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379")


@pytest.fixture(scope="function")
def client(policy_url, operator_ui_url, redis_url) -> SmartLoadClient:
    with SmartLoadClient(
        base_url=policy_url,
        operator_ui_url=operator_ui_url,
        redis_url=redis_url,
    ) as c:
        yield c


@pytest.fixture(scope="function")
def redis_client(redis_url):
    r = redis_lib.from_url(redis_url, decode_responses=False)
    yield r
    try:
        r.close()
    except Exception:
        pass


@pytest.fixture(scope="function")
def test_source() -> str:
    """Unique `source` marker per test so a publisher can distinguish its
    own envelopes from the real AI services' organic traffic on the same
    channels."""
    return f"e2e-live-engines-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="function")
def publish(redis_client, test_source):
    """Publish a typed envelope on `channel` with the test's unique source.

    Returns a callable: `publish(channel, payload) -> event_id`. The
    payload is either a dataclass instance or a plain dict.
    """
    def _publish(channel: str, payload: Any) -> str:
        return publish_envelope(
            redis_client,
            channel=channel,
            source=test_source,
            payload=payload,
        )
    return _publish
