"""
tests/integration/test_anomaly_isolation_forest.py
──────────────────────────────────────────────────────
Live-stack acceptance test for the IsolationForest anomaly engine
(PR #158 #4 / issue #101 AC#3).

Meaningful only when the anomaly-detector has loaded
ANOMALY_ENGINE=isolation_forest (the real .pkl bundle) -- the whole module is
skipped if /health reports the threshold engine or a fallback.

Validates the full loop end-to-end against a real container, a real
isolation_forest.pkl, and the Phase 1 stability gate + backend_health
persistence (services/anomaly-detector/runloop.py's apply_stability_gate):

  1. Inject latency (POST /_admin/delay {"ms": 800}) on one test-backend
     container via docker exec (see _chaos.py).
  2. Wait for the engine to observe and confirm the flip (>=
     flip_confirmation_cycles consecutive non-healthy cycles).
  3. Assert the backend's latest backend_health row has
     status in ("degraded", "unhealthy").
  4. Clear the delay ({"ms": 0}).
  5. Wait again and assert recovery back to "healthy".

Run:
    ANOMALY_ENGINE=isolation_forest docker compose up -d --build
    pytest tests/integration/test_anomaly_isolation_forest.py -v
    docker compose down
"""

from __future__ import annotations

import time

import docker as docker_sdk
import psycopg2
import pytest
import requests

from ._chaos import (
    ANOMALOUS_DELAY_MS,
    backend_container_names,
    backend_instance_id,
    set_backend_delay,
)
from .conftest import SERVICE_URLS, TIMESCALEDB_DSN

ANOMALY_DETECTOR_URL = SERVICE_URLS["anomaly-detector"]

# anomaly-detector defaults (services/anomaly-detector/app.py): a 60s rolling
# window queried every 10s. Detection needs at least one full window of
# delayed samples, then flip_confirmation_cycles consecutive cycles to confirm.
WINDOW_SECONDS = 60
POLL_INTERVAL_SECONDS = 10.0


@pytest.fixture(scope="module")
def docker_client(stack_ready):
    client = docker_sdk.from_env()
    yield client
    client.close()


@pytest.fixture(scope="module")
def db_conn(stack_ready):
    conn = psycopg2.connect(TIMESCALEDB_DSN)
    yield conn
    conn.close()


@pytest.fixture(scope="module", autouse=True)
def _require_isolation_forest(stack_ready):
    """Skip this whole module unless the live anomaly-detector has the
    isolation_forest engine loaded and ready."""
    resp = requests.get(f"{ANOMALY_DETECTOR_URL}/health", timeout=10)
    resp.raise_for_status()
    body = resp.json()
    if body.get("engine_type") != "isolation_forest" or not body.get("engine_ready"):
        pytest.skip(
            "anomaly-detector is not running the isolation_forest engine "
            f"(engine_type={body.get('engine_type')!r}, engine_ready={body.get('engine_ready')!r}). "
            "Set ANOMALY_ENGINE=isolation_forest and restart the stack to run this suite."
        )


@pytest.fixture(scope="module")
def flip_confirmation_cycles() -> int:
    resp = requests.get(f"{ANOMALY_DETECTOR_URL}/api/v1/engine/state", timeout=10)
    resp.raise_for_status()
    return int(resp.json()["policy_snapshot"]["flip_confirmation_cycles"])


def _latest_status(db_conn, backend_id: str) -> str | None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM backend_health WHERE backend_id = %s ORDER BY time DESC LIMIT 1;",
            (backend_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _wait_for_status(
    db_conn, backend_id: str, expected: set[str], timeout: float, interval: float = 5.0
) -> str | None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = _latest_status(db_conn, backend_id)
        if last in expected:
            return last
        time.sleep(interval)
    return last


def test_isolation_forest_detects_and_recovers(stack_ready, docker_client, db_conn, flip_confirmation_cycles):
    container_name = backend_container_names()[0]
    backend_id = backend_instance_id(docker_client, container_name)

    detection_timeout = WINDOW_SECONDS + (flip_confirmation_cycles + 2) * POLL_INTERVAL_SECONDS

    try:
        set_backend_delay(docker_client, container_name, ANOMALOUS_DELAY_MS)

        status = _wait_for_status(db_conn, backend_id, {"degraded", "unhealthy"}, timeout=detection_timeout)
        assert status in ("degraded", "unhealthy"), (
            f"expected {backend_id} to flip to degraded/unhealthy within "
            f"{detection_timeout:.0f}s of latency injection, last status was {status!r}"
        )
    finally:
        set_backend_delay(docker_client, container_name, 0)

    status = _wait_for_status(db_conn, backend_id, {"healthy"}, timeout=detection_timeout)
    assert status == "healthy", (
        f"expected {backend_id} to recover to healthy within {detection_timeout:.0f}s "
        f"of clearing the delay, last status was {status!r}"
    )
