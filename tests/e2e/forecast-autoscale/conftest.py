"""Pytest fixtures for the forecast-autoscale e2e suite.

The slice runs forecasting → autoscaler → operator surfaces, so observation
goes through the SmartLoad SDK (the customer surface). The forecast
injection is the one exception: there is no operator-facing "publish a
forecast" endpoint, so the suite publishes ForecastResult envelopes
straight to Redis to drive a deterministic predicted_rps. Mirrors
tests/e2e/manual-actions/conftest.py's SDK-client pattern, scoped to the
forecasting + autoscaler surfaces this feature manifest covers.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

try:  # raw Redis is needed only to inject forecasts; the suite skips without the stack anyway
    import redis as redis_lib
except ImportError:  # pragma: no cover
    redis_lib = None  # type: ignore

try:  # docker is best-effort: only used to reset the autoscaler cooldown timer
    import docker as docker_sdk
except ImportError:  # pragma: no cover
    docker_sdk = None  # type: ignore

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
    """Per-test SmartLoadClient. policy-manager + autoscaler are the HTTP
    surfaces; the SSE scale stream is reached through the operator-ui BFF
    (the SDK's default operator_ui_url).

    A generous timeout (30 s) because the autoscaler's scale endpoint blocks
    while it actuates containers (start/stop can take several seconds), well
    past the SDK's 10 s default."""
    with SmartLoadClient(
        base_url=policy_url,
        autoscaler_url=autoscaler_url,
        redis_url=redis_url,
        timeout=30.0,
    ) as c:
        yield c


@pytest.fixture(scope="function")
def redis_publisher(redis_url):
    """Raw Redis connection used to inject ForecastResult envelopes — the
    only surface in this suite that isn't the SDK, because forecasts are
    published by the forecasting service, not the operator."""
    if redis_lib is None:
        pytest.skip("redis client not installed; cannot inject forecasts")
    rclient = redis_lib.from_url(redis_url)
    yield rclient
    rclient.close()


@pytest.fixture(scope="function")
def reset_cooldown(autoscaler_url):
    """Return a callable that restarts the autoscaler so its in-memory
    cooldown timer starts clean, then waits for /health.

    Returned as a callable (not done at setup) so the test can establish
    pool headroom FIRST and only then reset the cooldown — otherwise the
    headroom scale would itself arm the cooldown the forecast needs clear.

    Best-effort: if the Docker SDK or socket is unavailable the call is a
    no-op — the forecast tests skip gracefully when no scale happens, so a
    missing reset degrades into a skip rather than a hang. Returns True if
    the autoscaler was restarted and came back healthy, else False."""
    import requests

    def _reset() -> bool:
        if docker_sdk is None:
            return False
        try:
            dclient = docker_sdk.from_env()
            container = dclient.containers.get("smartload-autoscaler-1")
            container.restart(timeout=5)
        except Exception:
            # No socket / wrong container name (compose project renamed):
            # leave the autoscaler as-is. The test handles a non-scaling
            # outcome by skipping.
            return False
        ok = False
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                resp = requests.get(f"{autoscaler_url}/health", timeout=2)
                if resp.status_code == 200:
                    ok = True
                    break
            except requests.RequestException:
                pass
            time.sleep(0.5)
        try:
            dclient.close()
        except Exception:
            pass
        return ok

    return _reset


@pytest.fixture(scope="function")
def baseline_count(client):
    """A reasonable starting backend count, used to seed each test's
    authoritative probe and to restore the cluster on teardown.

    Read from the most recent scaling-audit row (which can lag the live
    pool, so tests treat it as a seed and re-probe the real count from the
    autoscaler's own scale response). Mirrors
    tests/e2e/manual-actions/conftest.py's intent: leave no state behind."""
    rows = client.list_audit("scaling", limit=1)
    if rows:
        start = int(rows[0]["instance_count"])
    else:
        start = int(client.get_policy()["max_backends"])
    yield start
    try:
        client.scale(start, actor="e2e-fa-teardown", reason="restore baseline")
    except Exception:
        pass
