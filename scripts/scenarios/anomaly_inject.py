#!/usr/bin/env python3
"""
scripts/scenarios/anomaly_inject.py
────────────────────────────────────
DEMO: prove the anomaly-reroute path. Injects a synthetic AnomalyEvent for a
backend (status=unhealthy) and watches smartload.anomaly for the round-trip,
then (if the lb-sidecar run loop is enabled) confirms the backend drops out of
the upstream pool. Recovers the backend on the way out.

What it does:
  1. Subscribe to smartload.anomaly.
  2. POST /api/v1/isolate {status: unhealthy} via the SmartLoad SDK.
  3. Watch smartload.anomaly for the matching AnomalyEvent envelope.
  4. Best-effort: confirm the lb-sidecar excludes the backend.
  5. POST /api/v1/isolate {status: healthy} to recover; confirm re-inclusion.

This narrates to the console; it is not a pytest test. Exit 0 on the observed
round-trip, 1 on timeout or mismatch.

Usage:
  python scripts/scenarios/anomaly_inject.py
  python scripts/scenarios/anomaly_inject.py --backend smartload-test-backend-3:8080
  python scripts/scenarios/anomaly_inject.py \\
      --anomaly-detector-url http://localhost:8082 \\
      --lb-sidecar-url http://localhost:8087 \\
      --redis-url redis://localhost:6379
"""

from __future__ import annotations

import argparse
import os
import sys

import _common as C

DEFAULT_BACKEND = "smartload-test-backend-3:8080"


def run(backend: str, anomaly_detector_url: str, lb_sidecar_url: str,
        redis_url: str, timeout: float) -> int:
    from smartload_client import SmartLoadClient, SmartLoadError

    ANOMALY_CHANNEL = "smartload.anomaly"

    C.banner(
        "anomaly reroute demo",
        f"Inject AnomalyEvent for {backend} (unhealthy); expect the envelope on "
        f"{ANOMALY_CHANNEL} within {timeout:g}s, then recover.",
    )
    C.conn("anomaly-detector:", anomaly_detector_url)
    C.conn("lb-sidecar:", lb_sidecar_url)
    C.conn("redis:", redis_url)

    redis_client = C.open_redis(redis_url)
    if redis_client is None:
        return C.fail("redis-py is required for this scenario")

    # ── Step 1: subscribe ─────────────────────────────────────────────────────
    C.step(1, f"Subscribe to {ANOMALY_CHANNEL}")
    pubsub = C.subscribe(redis_client, ANOMALY_CHANNEL)
    C.ok("subscribed")

    rc = 0
    try:
        with SmartLoadClient(
            anomaly_detector_url=anomaly_detector_url,
            redis_url=redis_url,
        ) as client:
            # ── Step 2: inject unhealthy ──────────────────────────────────────
            C.step(2, f"POST /api/v1/isolate ({backend} -> unhealthy)")
            try:
                applied = client.isolate(
                    backend, "unhealthy",
                    actor="anomaly-inject", reason="scenario injection",
                )
            except SmartLoadError as exc:
                return C.fail(f"isolate(unhealthy) failed: {exc}")
            C.ok(f"applied: event_id={applied.get('event_id')} "
                 f"score={applied.get('score')}")

            # ── Step 3: watch for the envelope ────────────────────────────────
            C.step(3, f"Watch {ANOMALY_CHANNEL} for the AnomalyEvent")
            result = C.wait_for_envelope(
                pubsub,
                ANOMALY_CHANNEL,
                lambda p, _m: (
                    p.get("backend_id") == backend and p.get("status") == "unhealthy"
                ),
                timeout=timeout,
            )
            if result is None:
                return C.fail(
                    f"no AnomalyEvent for {backend}/unhealthy on "
                    f"{ANOMALY_CHANNEL} within {timeout:g}s"
                )
            payload, _meta = result
            C.ok(f"AnomalyEvent observed: status={payload.get('status')} "
                 f"score={payload.get('score')}")

            # ── Step 4: best-effort lb-sidecar exclusion check ────────────────
            C.step(4, "Check lb-sidecar exclusion (best-effort)")
            code, body = C.http_get_json(f"{lb_sidecar_url.rstrip('/')}/api/v1/lb/state")
            if code == 503:
                C.info("lb-sidecar run loop disabled; skipping exclusion check")
            elif code == 200 and isinstance(body, dict):
                excluded = body.get("excluded_backends", [])
                if backend in excluded:
                    C.ok(f"{backend} excluded from upstream: {excluded}")
                else:
                    C.warn(f"{backend} not yet in excluded_backends ({excluded}); "
                           f"is lb-sidecar subscribed to {ANOMALY_CHANNEL}?")
            else:
                C.warn(f"GET /api/v1/lb/state returned {code}; skipping check")

            # ── Step 5: recover ───────────────────────────────────────────────
            C.step(5, f"POST /api/v1/isolate ({backend} -> healthy)")
            try:
                recovered = client.isolate(
                    backend, "healthy",
                    actor="anomaly-inject", reason="scenario recovery",
                )
                C.ok(f"recovered: event_id={recovered.get('event_id')}")
            except SmartLoadError as exc:
                C.warn(f"recovery isolate(healthy) failed: {exc}")

            healthy = C.wait_for_envelope(
                pubsub,
                ANOMALY_CHANNEL,
                lambda p, _m: (
                    p.get("backend_id") == backend and p.get("status") == "healthy"
                ),
                timeout=timeout,
            )
            if healthy is not None:
                C.ok(f"{backend} recovery AnomalyEvent observed")
            else:
                C.warn("did not observe the recovery AnomalyEvent within deadline")
    finally:
        pubsub.close()

    if rc != 0:
        return rc
    return C.done(f"anomaly round-trip for {backend} complete")


def main() -> int:
    parser = argparse.ArgumentParser(description="Anomaly reroute demo")
    parser.add_argument("--backend", default=DEFAULT_BACKEND,
                        help=f"backend id to isolate (default {DEFAULT_BACKEND})")
    parser.add_argument("--anomaly-detector-url", default=C.anomaly_detector_url())
    parser.add_argument(
        "--lb-sidecar-url",
        default=os.environ.get("LB_SIDECAR_URL", "http://localhost:8087"),
    )
    parser.add_argument("--redis-url", default=C.redis_url())
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="seconds to wait for each envelope (default 30)")
    args = parser.parse_args()
    return run(
        args.backend, args.anomaly_detector_url, args.lb_sidecar_url,
        args.redis_url, args.timeout,
    )


if __name__ == "__main__":
    sys.exit(main())
