"""
examples/scenarios/anomaly-detection/anomaly_walk.py
────────────────────────────────────────────────────────
Proves the anomaly-detection slice end-to-end (docs/features/anomaly-detection.md).

Steps:
  1. Check anomaly-detector /health — report engine_type/engine_ready/engine_requested.
  2. Read /api/v1/engine/state — confirm policy_snapshot exposes the Phase-1
     stability-gate fields (flip_confirmation_cycles, min_sample_count).
  3. Subscribe to smartload.anomaly via Redis.
  4. POST /api/v1/isolate {"status": "unhealthy"} via the SDK — confirm the
     applied response, the published AnomalyEvent, and (if lb-sidecar's run
     loop is enabled) that the backend is excluded from /api/v1/lb/state.
  5. POST /api/v1/isolate {"status": "healthy"} — confirm recovery and
     re-inclusion.

Exit code:
  0 — all required steps passed
  1 — failure or timeout

Usage:
  python examples/scenarios/anomaly-detection/anomaly_walk.py
  python examples/scenarios/anomaly-detection/anomaly_walk.py \\
      --anomaly-detector-url http://localhost:8082 \\
      --lb-sidecar-url http://localhost:8087 \\
      --redis-url redis://localhost:6379
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

from smartload_client import SmartLoadClient, SmartLoadError

BACKEND_ID = "smartload-test-backend-3:8080"

SSE_DEADLINE_SECONDS = 30.0


def _ok(msg: str) -> None:
    print(f"  OK  {msg}")


def _fail(msg: str) -> int:
    print(f"FAIL  {msg}", file=sys.stderr)
    return 1


def _step(n: int, title: str) -> None:
    print(f"\nStep {n}: {title}")


def _wait_for_anomaly_event(pubsub, backend_id: str, status: str, timeout: float) -> dict | None:
    """Block on pubsub for up to timeout; return the first AnomalyEvent
    payload matching backend_id + status, or None on timeout."""
    import json

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if message is None or message.get("type") != "message":
            continue
        try:
            envelope = json.loads(message["data"])
        except (TypeError, ValueError):
            continue
        payload = envelope.get("payload", {})
        if payload.get("backend_id") == backend_id and payload.get("status") == status:
            return payload
    return None


def run(anomaly_detector_url: str, lb_sidecar_url: str, redis_url: str) -> int:
    http = httpx.Client(base_url=anomaly_detector_url.rstrip("/"), timeout=30.0)

    # ── Step 1: health ────────────────────────────────────────────────────────
    _step(1, "Check anomaly-detector /health")
    r = http.get("/health")
    if r.status_code not in (200, 503):
        return _fail(f"/health returned {r.status_code}: {r.text[:200]}")
    body = r.json()
    _ok(
        f"status={body.get('status')} engine_type={body.get('engine_type')!r} "
        f"engine_ready={body.get('engine_ready')!r} engine_requested={body.get('engine_requested')!r}"
    )

    # ── Step 2: engine state / policy snapshot ──────────────────────────────────
    _step(2, "Read /api/v1/engine/state")
    r = http.get("/api/v1/engine/state")
    if r.status_code != 200:
        return _fail(f"GET /api/v1/engine/state returned {r.status_code}: {r.text[:200]}")
    state = r.json()
    policy = state.get("policy_snapshot", {})
    for key in ("flip_confirmation_cycles", "min_sample_count", "anomaly_response"):
        if key not in policy:
            return _fail(f"policy_snapshot missing {key!r}: {policy}")
    _ok(
        f"runloop_enabled={state.get('runloop_enabled')} "
        f"flip_confirmation_cycles={policy['flip_confirmation_cycles']} "
        f"min_sample_count={policy['min_sample_count']} "
        f"anomaly_response={policy['anomaly_response']!r}"
    )

    # ── Step 3: subscribe to smartload.anomaly ──────────────────────────────────
    _step(3, "Subscribe to smartload.anomaly")
    try:
        import redis as redis_lib
    except ImportError:
        print("  SKIP (redis-py not installed) -- steps 4-5 will skip envelope checks")
        redis_lib = None
        pubsub = None
    else:
        r_client = redis_lib.from_url(redis_url, decode_responses=True)
        pubsub = r_client.pubsub()
        pubsub.subscribe("smartload.anomaly")
        pubsub.get_message(timeout=1.0)  # consume the subscribe confirmation
        _ok("subscribed")

    with SmartLoadClient(
        anomaly_detector_url=anomaly_detector_url,
        redis_url=redis_url,
    ) as client:

        # ── Step 4: isolate as unhealthy ────────────────────────────────────────
        _step(4, f"POST /api/v1/isolate ({BACKEND_ID} -> unhealthy)")
        try:
            r4 = client.isolate(BACKEND_ID, "unhealthy", actor="anomaly-walk", reason="scenario walkthrough")
        except SmartLoadError as exc:
            return _fail(f"isolate(unhealthy) failed: {exc}")
        if r4["status"] != "applied" or r4["anomaly_status"] != "unhealthy" or r4["score"] != 1.0:
            return _fail(f"unexpected isolate(unhealthy) response: {r4}")
        _ok(f"applied: event_id={r4['event_id']}")

        if pubsub is not None:
            payload = _wait_for_anomaly_event(pubsub, BACKEND_ID, "unhealthy", SSE_DEADLINE_SECONDS)
            if payload is None:
                return _fail(
                    f"no smartload.anomaly envelope for {BACKEND_ID}/unhealthy "
                    f"within {SSE_DEADLINE_SECONDS}s"
                )
            _ok(f"received AnomalyEvent: {payload}")

        lb_http = httpx.Client(base_url=lb_sidecar_url.rstrip("/"), timeout=30.0)
        lb_r = lb_http.get("/api/v1/lb/state")
        if lb_r.status_code == 503:
            print("  SKIP lb-sidecar exclusion check (run loop disabled)")
        elif lb_r.status_code != 200:
            return _fail(f"GET /api/v1/lb/state returned {lb_r.status_code}")
        else:
            excluded = lb_r.json().get("excluded_backends", [])
            if BACKEND_ID in excluded:
                _ok(f"{BACKEND_ID} excluded from upstream: {excluded}")
            else:
                print(f"  WARN: {BACKEND_ID} not in excluded_backends ({excluded}) -- "
                      f"is lb-sidecar subscribed to smartload.anomaly?")

        # ── Step 5: recover ──────────────────────────────────────────────────────
        _step(5, f"POST /api/v1/isolate ({BACKEND_ID} -> healthy)")
        try:
            r5 = client.isolate(BACKEND_ID, "healthy", actor="anomaly-walk", reason="scenario recovery")
        except SmartLoadError as exc:
            return _fail(f"isolate(healthy) failed: {exc}")
        if r5["status"] != "applied" or r5["anomaly_status"] != "healthy" or r5["score"] != 0.0:
            return _fail(f"unexpected isolate(healthy) response: {r5}")
        _ok(f"applied: event_id={r5['event_id']}")

        if pubsub is not None:
            payload = _wait_for_anomaly_event(pubsub, BACKEND_ID, "healthy", SSE_DEADLINE_SECONDS)
            if payload is None:
                return _fail(
                    f"no smartload.anomaly envelope for {BACKEND_ID}/healthy "
                    f"within {SSE_DEADLINE_SECONDS}s"
                )
            _ok(f"received AnomalyEvent: {payload}")
            pubsub.close()

        lb_r = lb_http.get("/api/v1/lb/state")
        if lb_r.status_code == 200:
            excluded = lb_r.json().get("excluded_backends", [])
            if BACKEND_ID not in excluded:
                _ok(f"{BACKEND_ID} re-included in upstream: excluded={excluded}")
            else:
                print(f"  WARN: {BACKEND_ID} still in excluded_backends ({excluded})")

    print("\nOK anomaly-detection scenario complete")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="anomaly-detection slice walkthrough")
    parser.add_argument(
        "--anomaly-detector-url",
        default=os.environ.get("SMARTLOAD_ANOMALY_DETECTOR_URL", "http://localhost:8082"),
    )
    parser.add_argument(
        "--lb-sidecar-url",
        default=os.environ.get("LB_SIDECAR_URL", "http://localhost:8087"),
    )
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("REDIS_URL", "redis://localhost:6379"),
    )
    args = parser.parse_args()
    return run(args.anomaly_detector_url, args.lb_sidecar_url, args.redis_url)


if __name__ == "__main__":
    sys.exit(main())
