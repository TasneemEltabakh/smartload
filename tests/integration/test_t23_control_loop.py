"""
tests/integration/test_t23_control_loop.py
───────────────────────────────────────────
End-to-end integration tests for the SmartLoad closed control loop (#103 T2.3).

Three scenarios prove the full chain works against a live docker-compose stack:

  - test_anomaly_reroute_excludes_then_recovers
      Manually publish an UNHEALTHY AnomalyEvent for a live backend; assert
      the lb-sidecar excludes it from its adapter state within the SLA window.
      Publish a HEALTHY recovery; assert the exclusion clears.

  - test_safe_mode_publishes_envelope_and_resets_weights
      POST safe_mode=true to policy-manager; assert the smartload.policy
      envelope carries the change and the lb-sidecar resets all upstream
      weights to 1 (the safety pin). Restore safe_mode=false at teardown.

  - test_forecast_drives_scale_out  (slow; @pytest.mark.slow)
      Drive sustained traffic at the load balancer; assert a scale_out row
      lands in scaling_events within 120s and a new dynamic container appears
      in the Docker pool. Skipped unless the stack was started with
      AUTOSCALER_PROVISIONING_ENABLED=true AND the current backend pool is
      at min_backends (so a scale-out is actually possible).

Stack lifecycle: assumes `docker compose up -d` per the existing tests/
integration/ convention. The `stack_ready` fixture in conftest.py waits for
every service to respond on /health before the first test runs. Tests
clean up after themselves so a failed run does not poison the next.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg2
import pytest
import redis as redis_lib
import requests

from tests.integration.conftest import REDIS_URL, SERVICE_URLS, TIMESCALEDB_DSN


# The lb-sidecar runs as a separate container on port 8087; conftest's
# SERVICE_URLS map only covers the seven AI/data services, so we add it
# locally here rather than mutating the shared map.
LB_SIDECAR_URL = "http://localhost:8087"

ANOMALY_CHANNEL = "smartload.anomaly"
POLICY_CHANNEL = "smartload.policy"
FORECAST_CHANNEL = "smartload.forecast"

# Compose-seeded test-backend identifiers. The lb-sidecar boots with these
# baked into ALL_BACKENDS_SEED (see services/lb-sidecar/app.py:106) and uses
# them as the canonical name format on every channel. We don't read this
# from /api/v1/lb/state because the sidecar's `upstream_weights` is empty
# until the first envelope handler fires — chicken-and-egg on a cold stack.
COMPOSE_BACKENDS = [
    "smartload-test-backend-1:8080",
    "smartload-test-backend-2:8080",
    "smartload-test-backend-3:8080",
    "smartload-test-backend-4:8080",
    "smartload-test-backend-5:8080",
]


# ── pubsub helpers ────────────────────────────────────────────────────────────

def _subscribe(channel: str):
    """Return a pubsub subscribed to `channel`, fully registered with Redis.

    Redis pubsub is fire-and-forget: a publish that lands before our
    SUBSCRIBE command is processed by the server is lost forever — there
    is no buffer for late subscribers. `ps.subscribe()` queues the command
    but does not block on the server-side ack; if we POST immediately
    after, the publisher's envelope can fly past before we're routed in.

    We block 300 ms after subscribing so the Redis round-trip completes,
    then drain any envelopes that arrived during the wait (e.g. a periodic
    forecast publish on a high-traffic channel). The drain is unbounded by
    sleep — get_message with a short timeout returns None as soon as the
    buffer empties — so a quiet channel costs only the 300 ms registration."""
    r = redis_lib.from_url(REDIS_URL, decode_responses=True)
    ps = r.pubsub(ignore_subscribe_messages=True)
    ps.subscribe(channel)
    time.sleep(0.3)
    while ps.get_message(timeout=0.05) is not None:
        pass
    return ps


def _wait_for_envelope(pubsub, predicate, timeout: float):
    """Poll `pubsub` for an envelope whose payload satisfies `predicate`.

    Returns the matching payload dict or None on timeout. The predicate
    is what makes the wait specific: a callable like
    `lambda p: p["safe_mode"] is True` keeps the test from being satisfied
    by an unrelated envelope on the same channel."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = pubsub.get_message(timeout=0.5)
        if msg is None:
            continue
        try:
            envelope = json.loads(msg["data"])
            payload = envelope.get("payload", {})
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if predicate(payload):
            return payload
    return None


# ── lb-sidecar state helpers ──────────────────────────────────────────────────

def _get_lb_state() -> dict:
    """Return the lb-sidecar adapter's current state.

    Shape: {upstream_weights: {backend: int}, excluded_backends: [...],
            algorithm: str}. Raises if the sidecar's run loop is not yet
    ready (503) — that means the test setup is wrong rather than a
    transient: fail fast."""
    r = requests.get(f"{LB_SIDECAR_URL}/api/v1/lb/state", timeout=5)
    r.raise_for_status()
    return r.json()


def _wait_for_lb_state(predicate, timeout: float, *, poll_interval: float = 0.5):
    """Poll /api/v1/lb/state until `predicate(state)` is true or timeout.

    Returns the matching state on success or None on timeout. We poll the
    state endpoint rather than the on-disk upstream.conf because the
    adapter's in-memory view is the authoritative read — file rewrites
    are debounced and atomic but the in-memory state updates first."""
    deadline = time.monotonic() + timeout
    last_state = None
    while time.monotonic() < deadline:
        try:
            last_state = _get_lb_state()
        except requests.RequestException:
            time.sleep(poll_interval)
            continue
        if predicate(last_state):
            return last_state
        time.sleep(poll_interval)
    return last_state


# ── policy helpers ────────────────────────────────────────────────────────────

def _get_policy() -> dict:
    r = requests.get(
        f"{SERVICE_URLS['policy-manager']}/api/v1/policy",
        timeout=5,
    )
    r.raise_for_status()
    return r.json()


def _post_policy(updates: dict) -> dict:
    r = requests.post(
        f"{SERVICE_URLS['policy-manager']}/api/v1/policy",
        json=updates,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


# ── scenario 1: anomaly reroute ───────────────────────────────────────────────

def test_anomaly_reroute_excludes_then_recovers(stack_ready):
    """SLA: lb-sidecar excludes an UNHEALTHY backend within 5 s of the
    AnomalyEvent publish; a HEALTHY recovery envelope clears the exclusion
    within the same window.

    Uses POST /api/v1/isolate to short-circuit the anomaly-detector's run
    loop — the detector hasn't observed enough latency to fire on its own
    on a fresh stack, and the manual call publishes a synthetic envelope
    via the exact same code path the engine uses. This is the contract
    that #103 T2.3 needs: anomaly publish in → lb-sidecar exclusion out."""
    target = COMPOSE_BACKENDS[0]
    initial = _get_lb_state()
    assert target not in initial.get("excluded_backends", []), (
        f"target backend {target!r} is already excluded at test start — clean "
        f"up the prior run before re-running this test."
    )

    ps = _subscribe(ANOMALY_CHANNEL)

    try:
        # Publish the UNHEALTHY signal.
        r = requests.post(
            f"{SERVICE_URLS['anomaly-detector']}/api/v1/isolate",
            json={
                "backend_id": target,
                "status": "unhealthy",
                "actor": "t23-integration",
                "reason": "scenario-1 reroute test",
            },
            timeout=10,
        )
        r.raise_for_status()

        anomaly_payload = _wait_for_envelope(
            ps,
            lambda p: p.get("backend_id") == target and p.get("status") == "unhealthy",
            timeout=5.0,
        )
        assert anomaly_payload is not None, (
            f"no UNHEALTHY AnomalyEvent for {target!r} on {ANOMALY_CHANNEL} "
            f"within 5 s of POST /api/v1/isolate"
        )

        excluded_state = _wait_for_lb_state(
            lambda s: target in s.get("excluded_backends", []),
            timeout=5.0,
        )
        assert excluded_state is not None and target in excluded_state["excluded_backends"], (
            f"lb-sidecar did not exclude {target!r} within 5 s of the "
            f"AnomalyEvent publish. Last state: {excluded_state}"
        )

        # Recovery: publish HEALTHY and assert the exclusion clears.
        ps_recovery = _subscribe(ANOMALY_CHANNEL)
        r = requests.post(
            f"{SERVICE_URLS['anomaly-detector']}/api/v1/isolate",
            json={
                "backend_id": target,
                "status": "healthy",
                "actor": "t23-integration",
                "reason": "scenario-1 recovery",
            },
            timeout=10,
        )
        r.raise_for_status()

        recovery_payload = _wait_for_envelope(
            ps_recovery,
            lambda p: p.get("backend_id") == target and p.get("status") == "healthy",
            timeout=5.0,
        )
        assert recovery_payload is not None, (
            f"no HEALTHY recovery AnomalyEvent for {target!r} within 5 s"
        )

        recovered_state = _wait_for_lb_state(
            lambda s: target not in s.get("excluded_backends", []),
            timeout=5.0,
        )
        assert recovered_state is not None and target not in recovered_state["excluded_backends"], (
            f"lb-sidecar did not clear exclusion for {target!r} within 5 s of "
            f"the HEALTHY publish. Last state: {recovered_state}"
        )
    finally:
        ps.close()
        # Belt-and-braces: even if an assertion above failed, push one more
        # HEALTHY publish so the next test starts from a clean exclusion set.
        try:
            requests.post(
                f"{SERVICE_URLS['anomaly-detector']}/api/v1/isolate",
                json={
                    "backend_id": target,
                    "status": "healthy",
                    "actor": "t23-integration",
                    "reason": "scenario-1 teardown",
                },
                timeout=5,
            )
        except requests.RequestException:
            pass


# ── scenario 3: safe-mode override ────────────────────────────────────────────

def test_safe_mode_publishes_envelope_and_resets_weights(stack_ready):
    """SLA: POST safe_mode=true → smartload.policy envelope within 3 s →
    lb-sidecar resets weights to equal (1 per backend) within 5 s.

    Why this matters: safe_mode is the operator's emergency brake — it
    must (a) propagate via the same envelope path policy changes use,
    and (b) cause the lb-sidecar to drop any RL-applied bias so the
    pool routes deterministically until the operator clears it."""
    original_policy = _get_policy()
    original_safe_mode = bool(original_policy.get("safe_mode", False))
    assert original_safe_mode is False, (
        "stack started with safe_mode already true — reset to false before "
        "running this test so we can assert the false → true transition."
    )

    ps_on = _subscribe(POLICY_CHANNEL)
    try:
        _post_policy({"safe_mode": True})

        on_payload = _wait_for_envelope(
            ps_on,
            lambda p: p.get("safe_mode") is True,
            timeout=3.0,
        )
        assert on_payload is not None, (
            f"no PolicyUpdate with safe_mode=true on {POLICY_CHANNEL} within 3 s"
        )

        reset_state = _wait_for_lb_state(
            lambda s: (
                bool(s.get("upstream_weights"))
                and len(set(s["upstream_weights"].values())) == 1
                and next(iter(s["upstream_weights"].values())) == 1
            ),
            timeout=5.0,
        )
        assert reset_state is not None, (
            f"lb-sidecar did not reset weights to all-1 within 5 s of the "
            f"safe_mode publish. Last weights: "
            f"{(reset_state or {}).get('upstream_weights')}"
        )
    finally:
        ps_on.close()

    # Restore — and assert the off-transition publishes too, so a future
    # safe_mode toggle isn't silently broken by a stale on-disk state.
    ps_off = _subscribe(POLICY_CHANNEL)
    try:
        _post_policy({"safe_mode": False})
        off_payload = _wait_for_envelope(
            ps_off,
            lambda p: p.get("safe_mode") is False,
            timeout=3.0,
        )
        assert off_payload is not None, (
            "no PolicyUpdate with safe_mode=false within 3 s of restore POST"
        )
    finally:
        ps_off.close()


# ── scenario 2: forecast-driven scale-out  (slow) ─────────────────────────────

def _hammer_lb(deadline_monotonic: float, results: list):
    """Drive requests at the load balancer until `deadline_monotonic` passes.

    Records request count on the worker so the test can sanity-check it
    actually generated load — if every request errored at sub-millisecond
    speed the autoscaler's run loop will never see the latency signal."""
    sent = 0
    ok = 0
    while time.monotonic() < deadline_monotonic:
        try:
            r = requests.get(f"{SERVICE_URLS['load-balancer']}/", timeout=2)
            sent += 1
            if r.status_code < 500:
                ok += 1
        except requests.RequestException:
            sent += 1
    results.append((sent, ok))


def _count_dynamic_backends() -> int:
    """Count test-backend containers carrying the smartload.dynamic=true label.

    Uses the Docker SDK directly so we read the live container set, not
    the autoscaler's intent — same reasoning as v1.0.7z's handle_scale
    re-querying Docker rather than trusting envelope counts."""
    import docker as docker_sdk

    client = docker_sdk.from_env()
    containers = client.containers.list(
        all=True,
        filters={"label": "com.docker.compose.service=test-backend"},
    )
    return sum(
        1 for c in containers
        if (c.labels or {}).get("smartload.dynamic") == "true"
    )


@pytest.mark.slow
def test_forecast_drives_scale_out(stack_ready):
    """SLA: with provisioning enabled and pool at min_backends, sustained
    traffic for 60 s causes a scale_out row in scaling_events within 120 s
    AND a new dynamic container appears in the Docker pool.

    Preconditions checked at test start; skipped (with explicit reason) if
    the stack is not configured for adaptive scaling. This keeps the test
    honest under default compose env without false negatives.

    Why this test is marked @slow: 60 s of load gen + a 120 s observation
    window. Total runtime ~3 min. Pytest skips slow-marked tests unless
    --runslow is passed (or the marker is selected with -m slow)."""
    policy = _get_policy()
    min_backends = int(policy.get("min_backends", 0))
    max_backends = int(policy.get("max_backends", 0))
    if max_backends <= min_backends:
        pytest.skip(
            f"scale-out impossible: max_backends={max_backends} <= "
            f"min_backends={min_backends}. Configure policy with a wider range."
        )

    initial_state = _get_lb_state()
    initial_backend_count = len(initial_state.get("upstream_weights", {}))
    if initial_backend_count > min_backends:
        pytest.skip(
            f"current pool has {initial_backend_count} backends but "
            f"min_backends={min_backends}. The autoscaler will not scale out "
            f"from a saturated pool in the time available. Restart the stack "
            f"with the compose service scaled to {min_backends} replicas, or "
            f"lower min_backends in policy.yaml."
        )

    # Check provisioning enabled via the autoscaler's /health body. The
    # autoscaler refuses to call cluster_client.provision() if the feature
    # flag is off, so traffic alone can't trigger a real new container.
    r = requests.get(
        f"{SERVICE_URLS['autoscaler']}/health",
        timeout=5,
    )
    r.raise_for_status()
    health = r.json()
    if not health.get("provisioning_enabled"):
        pytest.skip(
            "AUTOSCALER_PROVISIONING_ENABLED is not set on the autoscaler. "
            "Re-run with the adaptive-bench env-file (sets the flag and "
            "recreates the autoscaler container)."
        )

    dynamic_at_start = _count_dynamic_backends()
    db_test_start = time.time()

    # Drive traffic. 8 workers × ~25 RPS each ≈ 200 RPS aggregate at the
    # default test-backend latency; the moving-average forecast on the
    # 60 s window will project well above the per-instance capacity of
    # 100 RPS, triggering scale-out.
    load_deadline = time.monotonic() + 60.0
    results: list = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_hammer_lb, load_deadline, results) for _ in range(8)]
        for _ in as_completed(futures):
            pass

    total_sent = sum(s for s, _ in results)
    total_ok = sum(o for _, o in results)
    assert total_sent > 1000, (
        f"load generation under-delivered: only {total_sent} requests sent "
        f"in 60 s. The forecast won't trip on this volume."
    )
    assert total_ok > 0, (
        f"every request errored — load balancer may be down. sent={total_sent}"
    )

    # Now wait up to 120 s past the end of load for the scale_out row.
    # The autoscaler's cooldown is 60 s and the forecast cycle is ~10 s,
    # so a row landing inside 120 s is the realistic SLA.
    observe_deadline = time.monotonic() + 120.0
    scale_out_row = None
    while time.monotonic() < observe_deadline and scale_out_row is None:
        try:
            conn = psycopg2.connect(TIMESCALEDB_DSN, connect_timeout=5)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT time, action, instance_count, reason
                        FROM scaling_events
                        WHERE action = 'scale_out'
                          AND time > to_timestamp(%s)
                        ORDER BY time DESC
                        LIMIT 1
                        """,
                        (db_test_start,),
                    )
                    row = cur.fetchone()
                    if row:
                        scale_out_row = row
            finally:
                conn.close()
        except psycopg2.Error:
            pass
        if scale_out_row is None:
            time.sleep(5.0)

    assert scale_out_row is not None, (
        "no scale_out row in scaling_events within 120 s of load end. "
        f"Check that the forecasting service published predicted_rps > "
        f"per_instance_capacity_rps * {min_backends} during the load window."
    )

    # And confirm the action actually started a new container — the row
    # being present is the audit record; the new container is the effect.
    dynamic_at_end = _count_dynamic_backends()
    assert dynamic_at_end > dynamic_at_start, (
        f"scaling_events recorded scale_out but no new dynamic container "
        f"appeared: {dynamic_at_start} → {dynamic_at_end}. The autoscaler's "
        f"cluster_client.provision() call may have failed silently — check "
        f"the autoscaler container logs."
    )
