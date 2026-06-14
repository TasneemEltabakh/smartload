#!/usr/bin/env python3
"""
scripts/scenarios/scale_to_n.py
────────────────────────────────
DEMO: prove the manual scale action. POSTs /api/v1/scale on the autoscaler with
a target backend count, watches smartload.scale for the resulting ScalingEvent,
and restores the starting count on the way out.

What it does:
  1. Read the policy bounds [min_backends, max_backends] via the SDK.
  2. Snapshot the current backend count from the latest scaling audit row.
  3. Subscribe to smartload.scale.
  4. POST /api/v1/scale {target_count: N} via the SDK.
  5. Watch smartload.scale for the matching ScalingEvent.
  6. Restore the starting count (best-effort).

If --target is omitted the script picks an in-band count different from the
current one so the demo always actuates something.

This narrates to the console; it is not a pytest test. Exit 0 on observed
scale, 1 on timeout or mismatch.

Usage:
  python scripts/scenarios/scale_to_n.py --target 4
  python scripts/scenarios/scale_to_n.py            # auto-pick an in-band target
  python scripts/scenarios/scale_to_n.py \\
      --policy-url http://localhost:8086 \\
      --autoscaler-url http://localhost:8085 \\
      --redis-url redis://localhost:6379
"""

from __future__ import annotations

import argparse
import sys

import _common as C


def run(target: int | None, policy_url: str, autoscaler_url: str,
        redis_url: str, timeout: float) -> int:
    from smartload_client import SmartLoadClient, SmartLoadError, ValidationError

    SCALE_CHANNEL = "smartload.scale"

    C.banner(
        "manual scale demo",
        f"POST /api/v1/scale; expect a ScalingEvent on {SCALE_CHANNEL} within "
        f"{timeout:g}s, then restore the starting count.",
    )
    C.conn("policy-manager:", policy_url)
    C.conn("autoscaler:", autoscaler_url)
    C.conn("redis:", redis_url)

    redis_client = C.open_redis(redis_url)
    if redis_client is None:
        return C.fail("redis-py is required for this scenario")

    with SmartLoadClient(
        base_url=policy_url,
        autoscaler_url=autoscaler_url,
        redis_url=redis_url,
    ) as client:
        # ── Step 1: read bounds ───────────────────────────────────────────────
        C.step(1, "Read policy bounds")
        try:
            policy = client.get_policy()
        except SmartLoadError as exc:
            return C.fail(f"could not read policy: {exc}")
        min_b = int(policy["min_backends"])
        max_b = int(policy["max_backends"])
        C.ok(f"bounds [{min_b}, {max_b}] (policy_version={policy.get('policy_version')})")

        # ── Step 2: snapshot current count ────────────────────────────────────
        C.step(2, "Snapshot current backend count")
        try:
            rows = client.list_audit("scaling", limit=1)
        except SmartLoadError as exc:
            return C.fail(f"could not read scaling audit: {exc}")
        current = int(rows[0]["instance_count"]) if rows else max_b
        current = max(min_b, min(current, max_b))
        C.ok(f"current backend count: {current}")

        # Resolve target.
        if target is None:
            target = current + 1 if current < max_b else max(min_b, current - 1)
            C.info(f"no --target given; auto-picked {target}")
        if not (min_b <= target <= max_b):
            return C.fail(f"target {target} is out of band [{min_b}, {max_b}]")

        # ── Step 3: subscribe ─────────────────────────────────────────────────
        C.step(3, f"Subscribe to {SCALE_CHANNEL}")
        pubsub = C.subscribe(redis_client, SCALE_CHANNEL)
        C.ok("subscribed")

        rc = 0
        try:
            # ── Step 4: scale ─────────────────────────────────────────────────
            C.step(4, f"POST /api/v1/scale ({current} -> {target})")
            try:
                applied = client.scale(target, actor="scale-to-n", reason="scenario scale")
            except ValidationError as exc:
                return C.fail(f"scale rejected (field={exc.field}): {exc}")
            except SmartLoadError as exc:
                return C.fail(f"scale failed: {exc}")
            C.ok(f"status={applied.get('status')} action={applied.get('action')} "
                 f"previous={applied.get('previous_count')} -> "
                 f"final={applied.get('final_count')} "
                 f"event_id={applied.get('event_id')}")

            if applied.get("status") == "noop":
                C.info("scale was a no-op (already at target); skipping channel watch")
            else:
                # ── Step 5: watch smartload.scale ─────────────────────────────
                C.step(5, f"Watch {SCALE_CHANNEL} for the ScalingEvent")
                envelope = C.wait_for_envelope(
                    pubsub,
                    SCALE_CHANNEL,
                    lambda p, _m: p.get("instance_count") == target,
                    timeout=timeout,
                )
                if envelope is None:
                    rc = C.fail(
                        f"no ScalingEvent with instance_count={target} on "
                        f"{SCALE_CHANNEL} within {timeout:g}s"
                    )
                else:
                    payload, _meta = envelope
                    C.ok(f"scale event observed: action={payload.get('action')} "
                         f"instance_count={payload.get('instance_count')} "
                         f"reason={payload.get('reason')!r}")
        finally:
            # ── Step 6: restore ───────────────────────────────────────────────
            if target != current:
                C.step(6, f"Restore starting count ({target} -> {current})")
                try:
                    client.scale(current, actor="scale-to-n-restore", reason="restore")
                    C.ok("starting count restored")
                except SmartLoadError as exc:
                    C.warn(f"restore failed: {exc}")
            pubsub.close()

        if rc != 0:
            return rc

    return C.done("manual scale observed and restored")


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual scale demo")
    parser.add_argument("--target", type=int, default=None,
                        help="target backend count (default: auto-pick in-band)")
    parser.add_argument("--policy-url", default=C.policy_url())
    parser.add_argument("--autoscaler-url", default=C.autoscaler_url())
    parser.add_argument("--redis-url", default=C.redis_url())
    parser.add_argument("--timeout", type=float, default=15.0,
                        help="seconds to wait for the ScalingEvent (default 15)")
    args = parser.parse_args()
    return run(
        args.target, args.policy_url, args.autoscaler_url,
        args.redis_url, args.timeout,
    )


if __name__ == "__main__":
    sys.exit(main())
