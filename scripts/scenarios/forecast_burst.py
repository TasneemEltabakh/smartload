#!/usr/bin/env python3
"""
scripts/scenarios/forecast_burst.py
────────────────────────────────────
DEMO: prove forecast-driven scale-out. Publishes a high-RPS ForecastResult on
smartload.forecast and watches smartload.scale for the autoscaler's reaction.

What it does:
  1. Snapshot the autoscaler /health (current policy bounds + action counters).
  2. Subscribe to smartload.scale.
  3. Publish a ForecastResult (predicted_rps high enough to exceed cluster
     capacity) on smartload.forecast via the canonical Envelope helper.
  4. Watch for a ScalingEvent with action=scale_out within the deadline.
  5. Re-read /health and report the new action counters.

This narrates to the console; it is not a pytest test. Exit 0 on observed
scale-out, 1 on timeout or mismatch.

Usage:
  python scripts/scenarios/forecast_burst.py
  python scripts/scenarios/forecast_burst.py --predicted-rps 9999 --timeout 15
  python scripts/scenarios/forecast_burst.py \\
      --autoscaler-url http://localhost:8085 --redis-url redis://localhost:6379
"""

from __future__ import annotations

import argparse
import sys

import _common as C


def run(autoscaler_url: str, redis_url: str, predicted_rps: float,
        horizon_minutes: int, timeout: float) -> int:
    from services.shared.contracts import ForecastResult, publish_envelope

    FORECAST_CHANNEL = "smartload.forecast"
    SCALE_CHANNEL = "smartload.scale"

    C.banner(
        "forecast scale-out demo",
        f"Publish predicted_rps={predicted_rps:g} (horizon {horizon_minutes}m); "
        f"expect scale_out on {SCALE_CHANNEL} within {timeout:g}s.",
    )
    C.conn("autoscaler:", autoscaler_url)
    C.conn("redis:", redis_url)

    redis_client = C.open_redis(redis_url)
    if redis_client is None:
        return C.fail("redis-py is required for this scenario")

    # ── Step 1: snapshot autoscaler ───────────────────────────────────────────
    C.step(1, "Snapshot autoscaler /health")
    code, body = C.http_get_json(f"{autoscaler_url.rstrip('/')}/health")
    if code not in (200, 503) or not isinstance(body, dict):
        return C.fail(f"/health returned {code}: {str(body)[:200]}")
    policy = body.get("policy", {})
    stats_before = body.get("stats", {})
    max_backends = policy.get("max_backends")
    C.ok(
        f"status={body.get('status')} max_backends={max_backends} "
        f"scale_out_so_far={stats_before.get('actions_scale_out')}"
    )
    if isinstance(max_backends, int) and predicted_rps:
        cap = policy.get("per_instance_capacity_rps")
        if isinstance(cap, (int, float)) and cap > 0:
            full_cap = cap * max_backends
            if predicted_rps <= full_cap:
                C.warn(
                    f"predicted_rps {predicted_rps:g} <= full-cluster capacity "
                    f"{full_cap:g}; the autoscaler may not scale out. Raise "
                    f"--predicted-rps to force the demo."
                )

    # ── Step 2: subscribe to smartload.scale ──────────────────────────────────
    C.step(2, f"Subscribe to {SCALE_CHANNEL}")
    pubsub = C.subscribe(redis_client, SCALE_CHANNEL)
    C.ok("subscribed")

    try:
        # ── Step 3: publish the burst forecast ────────────────────────────────
        C.step(3, f"Publish ForecastResult on {FORECAST_CHANNEL}")
        forecast = ForecastResult(
            horizon_minutes=horizon_minutes,
            predicted_rps=predicted_rps,
            confidence_lower=predicted_rps * 0.9,
            confidence_upper=predicted_rps * 1.1,
            model_id="forecast_burst_demo",
        )
        event_id = publish_envelope(
            redis_client,
            channel=FORECAST_CHANNEL,
            source="forecast-burst-demo",
            payload=forecast,
        )
        C.ok(f"published forecast event_id={event_id}")

        # ── Step 4: wait for the scale event ──────────────────────────────────
        C.step(4, f"Watch {SCALE_CHANNEL} for scale_out (deadline {timeout:g}s)")
        result = C.wait_for_envelope(
            pubsub,
            SCALE_CHANNEL,
            lambda payload, _meta: payload.get("action") == "scale_out",
            timeout=timeout,
        )
        if result is None:
            return C.fail(
                f"no scale_out ScalingEvent on {SCALE_CHANNEL} within {timeout:g}s "
                f"(the cluster may already be at max_backends={max_backends})"
            )
        payload, meta = result
        C.ok(
            f"scale event observed: action={payload.get('action')} "
            f"instance_count={payload.get('instance_count')} "
            f"reason={payload.get('reason')!r} "
            f"(source={meta.get('source')})"
        )
    finally:
        pubsub.close()

    # ── Step 5: confirm the autoscaler counter moved ─────────────────────────
    C.step(5, "Re-read autoscaler /health")
    code, body = C.http_get_json(f"{autoscaler_url.rstrip('/')}/health")
    if code in (200, 503) and isinstance(body, dict):
        stats_after = body.get("stats", {})
        C.ok(f"scale_out counter now: {stats_after.get('actions_scale_out')}")
    else:
        C.warn(f"/health re-read returned {code}; skipping counter check")

    return C.done("forecast burst triggered scale-out")


def main() -> int:
    parser = argparse.ArgumentParser(description="Forecast scale-out demo")
    parser.add_argument("--autoscaler-url", default=C.autoscaler_url())
    parser.add_argument("--redis-url", default=C.redis_url())
    parser.add_argument("--predicted-rps", type=float, default=9999.0,
                        help="predicted RPS to publish (default 9999)")
    parser.add_argument("--horizon-minutes", type=int, default=5,
                        help="forecast look-ahead window in minutes (default 5)")
    parser.add_argument("--timeout", type=float, default=15.0,
                        help="seconds to wait for the scale event (default 15)")
    args = parser.parse_args()
    return run(
        args.autoscaler_url, args.redis_url,
        args.predicted_rps, args.horizon_minutes, args.timeout,
    )


if __name__ == "__main__":
    sys.exit(main())
