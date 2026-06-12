"""
tests/integration/test_isolation_forest_live_stack.py
──────────────────────────────────────────────────────
Live-stack end-to-end test for the Isolation Forest anomaly engine
(closes the acceptance-criterion gap noted on PR #158 / issue #101).

The other two test files for this engine cover different layers:

  - services/anomaly-detector/engines/isolation_forest/test_engine.py
    Pure-Python unit tests with a synthetic inline bundle (no .pkl, no
    docker, no real model). Fast feedback on the engine's score / status
    boundaries.

  - tests/integration/test_isolation_forest_artifact.py
    Smoke tests against the REAL shipped models/isolation_forest.pkl,
    loaded by the engine class. Catches sklearn-version drift between
    the runtime requirements pin and the training pin. Runs without
    docker.

This file is the missing piece: with the full docker compose stack up
AND the anomaly-detector container started with
ANOMALY_ENGINE=isolation_forest, inject latency on a single backend
via the test-backend's /_admin/delay knob and assert the trained model
publishes an UNHEALTHY envelope on smartload.anomaly within 2 monitor
intervals. This is what #101's acceptance criterion #3 actually asks
for in spirit, even though its literal text (FAIL_HEALTH=true) tests
the /health response path rather than the latency-driven inference
path that the engine consumes.

Marked @pytest.mark.slow because the natural runloop interval is 10 s
and the SQL window is 60 s — the test waits up to two cycles past the
end of a 30 s sustained-latency window for an UNHEALTHY publish.

Skipped with a clear reason when the stack is configured with a
different engine (the default is `threshold`); operators run this test
after re-creating the anomaly-detector container with
`ANOMALY_ENGINE=isolation_forest` in the env file.
"""

from __future__ import annotations

import subprocess
import time

import pytest
import requests

from tests.integration.conftest import REDIS_URL, SERVICE_URLS
from tests.integration.test_t23_control_loop import (
    ANOMALY_CHANNEL,
    COMPOSE_BACKENDS,
    _subscribe,
    _wait_for_envelope,
)


# The test-backend's port isn't host-published; /_admin/delay is reached via
# `docker exec` on the container, mirroring the pattern in
# experiments/adaptive-bench/anomaly_injector.py.
ANOMALY_LATENCY_MS = 400  # well above the 60 s rolling mean of an idle pool
ANOMALY_DURATION_S = 30   # at least three poll intervals of slow traffic
ASSERT_TIMEOUT_S = 35     # wait two more poll intervals past the load window

# The lb-sidecar isn't in conftest.SERVICE_URLS (it has no AI-service role); its
# weight/state API lives here.
LB_SIDECAR_URL = "http://localhost:8087"
# Raising rl_confidence_threshold to its ceiling makes the lb-sidecar REJECT
# every RL routing envelope ("confidence < threshold → using previous weights"),
# so the even weights we pin below actually stick — without flipping safe_mode
# (which would force the anomaly engine back to threshold and defeat the test).
# This makes the test independent of the live RL policy (active / shadow / a
# freshly-retrained model), which otherwise might down-weight the target backend
# to near zero and starve it of the samples the anomaly needs.
PIN_CONFIDENCE_THRESHOLD = 1.0


def _get_policy() -> dict:
    try:
        return requests.get(f"{SERVICE_URLS['policy-manager']}/api/v1/policy", timeout=5).json()
    except (requests.RequestException, ValueError):
        return {}


def _set_policy(updates: dict) -> None:
    try:
        requests.post(
            f"{SERVICE_URLS['policy-manager']}/api/v1/policy",
            json=updates, headers={"X-Actor": "live-stack-test"}, timeout=5,
        )
    except requests.RequestException:
        pass


def _running_upstream_backends() -> list[str]:
    """Backends currently in the NGINX upstream (lb-sidecar's view)."""
    try:
        state = requests.get(f"{LB_SIDECAR_URL}/api/v1/lb/state", timeout=5).json()
        return list((state.get("upstream_weights") or {}).keys())
    except (requests.RequestException, ValueError):
        return list(COMPOSE_BACKENDS)


def _set_even_weights(backends: list[str]) -> None:
    try:
        requests.post(f"{LB_SIDECAR_URL}/api/v1/lb/weights",
                      json={b: 1 for b in backends}, timeout=5)
    except requests.RequestException:
        pass


def _recover_backend(backend_id: str) -> None:
    """Clear any stale exclusion on the target so it's in the upstream pool.
    Publishes a `manual:` recovery — the test's predicate already filters those
    out, so this can't satisfy the assertion by itself."""
    try:
        requests.post(
            f"{SERVICE_URLS['anomaly-detector']}/api/v1/isolate",
            json={"backend_id": backend_id, "status": "healthy",
                  "actor": "live-stack-test", "reason": "clear stale exclusion"},
            timeout=5,
        )
    except requests.RequestException:
        pass


def _ensure_container_running(container: str) -> None:
    """Best-effort: start the target backend if the autoscaler scaled it out."""
    try:
        subprocess.run(["docker", "start", container], capture_output=True, timeout=15)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def _set_runtime_delay(container: str, ms: int) -> bool:
    """POST /_admin/delay {ms: <ms>} on `container` via `docker exec`.

    Returns True on success, False on any docker / curl failure. The
    test should not crash if cleanup-on-failure can't reach the container
    (the next test setup will reset the state)."""
    try:
        result = subprocess.run(
            [
                "docker", "exec", container,
                "curl", "-s", "-X", "POST",
                "-H", "Content-Type: application/json",
                "-d", f'{{"ms": {ms}}}',
                "http://localhost:8080/_admin/delay",
            ],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _drive_traffic(deadline_monotonic: float) -> int:
    """Drive a steady stream of GETs through the load balancer until the
    deadline. Returns the count of issued requests so the test can
    assert traffic actually flowed."""
    sent = 0
    while time.monotonic() < deadline_monotonic:
        try:
            requests.get(f"{SERVICE_URLS['load-balancer']}/", timeout=2)
        except requests.RequestException:
            pass
        sent += 1
    return sent


def _engine_in_use() -> str:
    """Read the anomaly-detector's /health to learn which engine is loaded.

    The /health body carries engine_type when the runloop is enabled
    (see services/anomaly-detector/app.py:_check_health). Returns the
    string or 'unknown' on any error."""
    try:
        r = requests.get(
            f"{SERVICE_URLS['anomaly-detector']}/health",
            timeout=5,
        )
        body = r.json()
        return body.get("engine_type", "unknown")
    except (requests.RequestException, ValueError):
        return "unknown"


@pytest.mark.slow
def test_isolation_forest_flags_slow_backend_unhealthy(stack_ready):
    """SLA: with ANOMALY_ENGINE=isolation_forest and the run-loop
    enabled, a single backend running 400 ms behind the rest for 30 s
    must surface as UNHEALTHY on smartload.anomaly within two further
    monitoring cycles (~20 s after load end).

    Why this is the test that matters for #101: the engine's own unit
    tests use a synthetic bundle, and the smoke tests in
    test_isolation_forest_artifact.py exercise the real .pkl in
    isolation. Neither validates the *closed loop* — that
    `services/anomaly-detector/runloop.py:_inference_cycle` actually
    feeds production-shape BackendFeatures into the trained model and
    that the resulting AnomalyScore makes it onto the bus.

    The engine was re-calibrated in production-shape space in v1.0.7ah
    (#165) — model + scaler co-located in one real-ms coordinate system —
    and the v1.0.7ai lb-otel-shipper IP→container-name fix makes the
    published `backend_id` match `target_backend_id`. This test pins
    routing itself (see the setup block) so it no longer depends on the
    live RL policy keeping the target backend weighted up. If it fails
    now, the realistic causes are a stuck runloop, a too-small SQL window,
    or the target container being scaled out before injection — not the
    model calibration."""
    engine = _engine_in_use()
    if engine != "isolation_forest":
        pytest.skip(
            f"anomaly-detector is running engine={engine!r}; this test "
            f"requires ANOMALY_ENGINE=isolation_forest. Re-create the "
            f"container with `ANOMALY_ENGINE=isolation_forest` in the "
            f"env file, then re-run with `pytest -m slow`."
        )

    target_container = "smartload-test-backend-1"
    target_backend_id = COMPOSE_BACKENDS[0]  # smartload-test-backend-1:8080

    # ── pin routing so the target keeps receiving traffic ─────────────────────
    # The engine only fires when the target's 60 s window holds enough slow
    # samples; if the live RL policy down-weights the target (it can, and a
    # retrained model will route differently again), it never gets them. Make
    # the loop deterministic without touching safe_mode: reject RL routing (so
    # our weights stick), ensure the target is running + un-excluded, then pin
    # even weights across the live pool. Restored in `finally`.
    original_threshold = float(_get_policy().get("rl_confidence_threshold", 0.6))
    _ensure_container_running(target_container)
    _set_policy({"rl_confidence_threshold": PIN_CONFIDENCE_THRESHOLD})
    _recover_backend(target_backend_id)
    time.sleep(2)  # let the policy + recovery envelopes reach the lb-sidecar
    backends = _running_upstream_backends()
    if target_backend_id not in backends:
        backends.append(target_backend_id)
    _set_even_weights(backends)
    time.sleep(1)

    # Subscribe BEFORE injecting; pubsub doesn't buffer for late subscribers.
    client, ps = _subscribe(ANOMALY_CHANNEL)

    try:
        injected = _set_runtime_delay(target_container, ANOMALY_LATENCY_MS)
        assert injected, (
            f"could not POST /_admin/delay to {target_container}; the "
            f"container may not be reachable from the test host's "
            f"docker daemon. Confirm `docker exec {target_container} "
            f"echo hi` works before re-running."
        )

        # Drive traffic for ANOMALY_DURATION_S so the slow backend
        # actually receives ~30 s of slow requests, populating the
        # anomaly-detector's 60 s SQL window with high-latency rows.
        deadline = time.monotonic() + ANOMALY_DURATION_S
        sent = _drive_traffic(deadline)
        assert sent > 50, (
            f"load generation issued only {sent} requests in "
            f"{ANOMALY_DURATION_S} s — the load balancer may be down "
            f"or the test host is under heavy CPU pressure."
        )

        # Now wait for the engine to publish UNHEALTHY for the target.
        # The runloop ticks every POLL_INTERVAL_SECONDS (default 10 s);
        # two more cycles is the realistic SLA after load ends.
        unhealthy_payload = _wait_for_envelope(
            ps,
            lambda p: (
                p.get("backend_id") == target_backend_id
                and p.get("status") == "unhealthy"
                and not str(p.get("model_version", "")).startswith("manual:")
            ),
            timeout=ASSERT_TIMEOUT_S,
        )
        assert unhealthy_payload is not None, (
            f"isolation_forest engine did not publish UNHEALTHY for "
            f"{target_backend_id!r} within {ASSERT_TIMEOUT_S} s of "
            f"{ANOMALY_DURATION_S} s of {ANOMALY_LATENCY_MS} ms "
            f"injected latency. Either the runloop is stuck, the SQL "
            f"window has too few samples, or the model's "
            f"production_scaler calibration doesn't separate this "
            f"latency band — see services/anomaly-detector/engines/"
            f"isolation_forest/README.md domain-adaptation caveat."
        )
        # The model_version filter on `manual:` excludes any synthetic
        # publishes from a prior test that left state behind — only
        # real engine inference satisfies the predicate.
    finally:
        # Always restore the backend to its baseline latency, even on
        # assertion failure, so subsequent tests don't inherit poison.
        _set_runtime_delay(target_container, 0)
        # Un-pin routing: hand control back to the RL engine.
        _set_policy({"rl_confidence_threshold": original_threshold})
        ps.close()
        client.close()
