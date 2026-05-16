"""
tests/integration/test_autoscaler.py
─────────────────────────────────────
End-to-end tests against the live docker-compose stack for the T1.3
autoscaler. Validates:

  1. A high-predicted_rps forecast on smartload.forecast produces a
     scale_out: a scaling_events row appears in TimescaleDB, a ScalingEvent
     envelope is published on smartload.scale, and the running test-backend
     count increases by 1.

  2. The cooldown is enforced: a second high forecast within the cooldown
     window does not produce another scaling event.

Test isolation:
  - Each test snapshots the scaling_events row count and running backend
    count BEFORE publishing, then asserts the delta. No global teardown
    of containers is performed — the next test starts from whatever state
    the previous test left, which is bounded by min/max anyway.
  - Forecasting service is still a stub (N1.2 not shipped), so this suite
    publishes ForecastResult envelopes directly to Redis from the test.

Run:
    docker compose up -d
    pytest tests/integration/test_autoscaler.py -v
    docker compose down
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict

import docker as docker_sdk
import psycopg2
import pytest
import redis as redis_lib
import requests

from services.shared.contracts import (
    ForecastResult,
    make_envelope,
    parse_envelope,
)

# Fixtures from conftest.py:
#   stack_ready  — waits for every service's /health
#   services     — name → base URL map
from .conftest import REDIS_URL, TIMESCALEDB_DSN

FORECAST_CHANNEL = "smartload.forecast"
SCALE_CHANNEL    = "smartload.scale"

# Headroom for the autoscaler control loop to consume the message and apply.
# LOOP_TICK_SECONDS in autoscaler is 5 s by default; we allow 3 × that.
SCALE_DEADLINE_SECONDS = 15.0


# ── helpers ───────────────────────────────────────────────────────────────────

def _publish_forecast(redis_client, predicted_rps: float, horizon_minutes: int = 5) -> str:
    """Publish a ForecastResult envelope; return the event_id."""
    payload = ForecastResult(
        horizon_minutes=horizon_minutes,
        predicted_rps=predicted_rps,
        confidence_lower=predicted_rps * 0.9,
        confidence_upper=predicted_rps * 1.1,
        model_id="test-harness",
    )
    envelope = make_envelope(source="test-autoscaler", payload=payload)
    redis_client.publish(FORECAST_CHANNEL, json.dumps(asdict(envelope)))
    return envelope.event_id


def _scaling_events_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM scaling_events;")
        return int(cur.fetchone()[0])


def _running_backend_count(docker_client) -> int:
    """Count test-backend containers in `running` state via the Docker socket.

    Mirrors what the autoscaler itself reads through DockerClusterClient —
    keeps the test grounded on the same source of truth as the system under
    test rather than `scaling_events` (which is empty before any action).
    """
    containers = docker_client.containers.list(
        filters={
            "label":  "com.docker.compose.service=test-backend",
            "status": "running",
        },
    )
    return len(containers)


def _set_backend_count(docker_client, target: int) -> None:
    """Stop or start test-backend containers so exactly `target` are running.

    Tests use this so each one starts from a known state — the cooldown test
    needs room to scale out twice, the scale-out test needs room to scale
    out once. Sorting by replica number matches what DockerClusterClient
    does internally so the autoscaler's subsequent action is deterministic.
    """
    all_containers = docker_client.containers.list(
        all=True,
        filters={"label": "com.docker.compose.service=test-backend"},
    )

    def _replica_n(c):
        import re
        m = re.search(r"-(\d+)$", c.name)
        return int(m.group(1)) if m else 0

    all_containers.sort(key=_replica_n)
    for i, container in enumerate(all_containers):
        should_run = i < target
        if should_run and container.status != "running":
            container.start()
        elif not should_run and container.status == "running":
            container.stop(timeout=5)

    # Brief settle so the next call to _running_backend_count sees the new state.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if _running_backend_count(docker_client) == target:
            return
        time.sleep(0.3)


def _wait_for_new_scale_event(
    pubsub,
    deadline_seconds: float,
    forecast_event_id: str,
) -> dict | None:
    """Block on pubsub for up to deadline_seconds; return the ScalingEvent
    payload triggered by `forecast_event_id`, or None on timeout."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if message is None or message.get("type") != "message":
            continue
        parsed = parse_envelope(message["data"], channel=SCALE_CHANNEL)
        if parsed is None:
            continue
        payload, meta = parsed
        if payload.get("forecast_event_id") == forecast_event_id:
            return payload
    return None


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def db_conn(stack_ready):
    conn = psycopg2.connect(TIMESCALEDB_DSN)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def docker_client(stack_ready):
    """Talks to the Docker socket the same way the autoscaler does."""
    client = docker_sdk.from_env()
    yield client
    client.close()


@pytest.fixture(scope="function")
def scale_subscriber(stack_ready):
    """A pubsub subscription to smartload.scale, drained of any pre-test
    backlog before yielding to the test."""
    client = redis_lib.from_url(REDIS_URL)
    pubsub = client.pubsub()
    pubsub.subscribe(SCALE_CHANNEL)
    # Drain whatever's already buffered so we only see events from this test.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1) is None:
            break
    yield pubsub
    pubsub.close()
    client.close()


@pytest.fixture(scope="function")
def redis_publisher(stack_ready):
    client = redis_lib.from_url(REDIS_URL)
    yield client
    client.close()


@pytest.fixture(scope="function")
def fresh_autoscaler(docker_client):
    """Restart the autoscaler container so its in-memory cooldown timer
    starts clean. Without this, a scale action in one test leaves
    _last_action_monotonic set, and the next test's first publish gets
    cooldown-suppressed for the wrong reason."""
    container = docker_client.containers.get("smartload-autoscaler-1")
    container.restart(timeout=5)
    # Wait for /health to come back so the control loop is subscribed by
    # the time we start publishing forecasts.
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            resp = requests.get("http://localhost:8085/health", timeout=2)
            if resp.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.5)
    pytest.fail("autoscaler did not become reachable after restart")


# ── tests ─────────────────────────────────────────────────────────────────────

class TestForecastDrivenScaling:

    def test_high_forecast_triggers_scale_out(
        self, db_conn, docker_client, redis_publisher, scale_subscriber,
        fresh_autoscaler,
    ):
        """A predicted_rps well above current capacity must produce one
        scaling_events row and one smartload.scale envelope tagged with the
        triggering forecast event_id."""
        _set_backend_count(docker_client, 3)
        before_count     = _scaling_events_count(db_conn)
        before_instances = _running_backend_count(docker_client)
        assert before_instances == 3, "fixture failed to bring stack to 3 backends"

        # 9999 rps blows past 3 backends × 100 capacity = 300 rps target.
        forecast_event_id = _publish_forecast(redis_publisher, predicted_rps=9999.0)
        scale_payload = _wait_for_new_scale_event(
            scale_subscriber, SCALE_DEADLINE_SECONDS, forecast_event_id,
        )

        assert scale_payload is not None, (
            "no smartload.scale envelope arrived within deadline — autoscaler "
            "may be in cooldown from a prior test, or not subscribed"
        )
        assert scale_payload["action"] == "scale_out"
        assert scale_payload["instance_count"] == before_instances + 1
        assert scale_payload["forecast_event_id"] == forecast_event_id

        # And the scaling_events table is the only source of truth (SOT §8.8).
        after_count = _scaling_events_count(db_conn)
        assert after_count == before_count + 1, (
            f"scaling_events row not inserted: {before_count} -> {after_count}"
        )

    def test_cooldown_suppresses_back_to_back_forecasts(
        self, db_conn, docker_client, redis_publisher, scale_subscriber,
        fresh_autoscaler,
    ):
        """Two high forecasts in quick succession produce exactly one
        scaling action. The second is dropped by the cooldown timer.

        Starts at 2 backends so both publishes have room to scale (capacity
        2×100 = 200; both publishes claim 9999 rps). The first MUST scale;
        the second MUST be cooldown-suppressed.
        """
        _set_backend_count(docker_client, 2)
        before_count = _scaling_events_count(db_conn)

        first_id  = _publish_forecast(redis_publisher, predicted_rps=9999.0)
        first_event = _wait_for_new_scale_event(
            scale_subscriber, SCALE_DEADLINE_SECONDS, first_id,
        )
        assert first_event is not None, "first forecast did not scale (fixture issue?)"
        assert first_event["action"] == "scale_out"

        # Immediately publish a second forecast; cooldown must suppress it.
        second_id = _publish_forecast(redis_publisher, predicted_rps=9999.0)
        suppressed = _wait_for_new_scale_event(
            scale_subscriber, SCALE_DEADLINE_SECONDS, second_id,
        )

        assert suppressed is None, (
            "second forecast produced a scale event during cooldown — "
            f"cooldown is not being enforced (event: {suppressed})"
        )

        # Exactly one new scaling_events row from this test.
        after_count = _scaling_events_count(db_conn)
        assert after_count - before_count == 1
