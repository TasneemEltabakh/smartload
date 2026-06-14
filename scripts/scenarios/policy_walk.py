#!/usr/bin/env python3
"""
scripts/scenarios/policy_walk.py
─────────────────────────────────
DEMO: prove the policy-change + audit story. Reads the current policy, applies a
short sequence of valid changes, watches each change land on smartload.policy,
restores the baseline, and prints the most recent policy_changes audit rows.

What it does:
  1. Snapshot the current policy via the SDK (GET /api/v1/policy).
  2. Subscribe to smartload.policy.
  3. Apply a sequence of valid field changes (slo_p95_latency_ms, safe_mode),
     watching for each matching PolicyUpdate envelope.
  4. Restore the baseline values.
  5. Print the latest policy audit rows so the demo shows the audit trail.

This narrates to the console; it is not a pytest test. Exit 0 when every change
round-trips through the bus, 1 on timeout or mismatch.

Usage:
  python scripts/scenarios/policy_walk.py
  python scripts/scenarios/policy_walk.py \\
      --policy-url http://localhost:8086 --redis-url redis://localhost:6379
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import _common as C


def run(policy_url: str, redis_url: str, timeout: float) -> int:
    from smartload_client import SmartLoadClient, SmartLoadError

    POLICY_CHANNEL = "smartload.policy"

    C.banner(
        "policy change + audit demo",
        f"Apply a sequence of valid policy changes; watch each on "
        f"{POLICY_CHANNEL}, restore the baseline, and show audit rows.",
    )
    C.conn("policy-manager:", policy_url)
    C.conn("redis:", redis_url)

    redis_client = C.open_redis(redis_url)
    if redis_client is None:
        return C.fail("redis-py is required for this scenario")

    with SmartLoadClient(base_url=policy_url, redis_url=redis_url) as client:
        # ── Step 1: snapshot baseline ─────────────────────────────────────────
        C.step(1, "Snapshot current policy")
        try:
            baseline = client.get_policy()
        except SmartLoadError as exc:
            return C.fail(f"could not read policy: {exc}")
        C.ok(f"baseline policy_version={baseline.get('policy_version')} "
             f"slo_p95_latency_ms={baseline.get('slo_p95_latency_ms')} "
             f"safe_mode={baseline.get('safe_mode')}")

        # Build a sequence of (field, new_value) that are valid but distinct from
        # baseline. slo_p95_latency_ms is a positive int; nudge it. safe_mode is
        # a bool; flip it.
        base_slo = int(baseline.get("slo_p95_latency_ms", 250) or 250)
        new_slo = base_slo + 50
        base_safe = bool(baseline.get("safe_mode", False))
        walk: list[tuple[str, Any]] = [
            ("slo_p95_latency_ms", new_slo),
            ("safe_mode", not base_safe),
        ]

        # ── Step 2: subscribe ─────────────────────────────────────────────────
        C.step(2, f"Subscribe to {POLICY_CHANNEL}")
        pubsub = C.subscribe(redis_client, POLICY_CHANNEL)
        C.ok("subscribed")

        rc = 0
        try:
            # ── Step 3: walk the changes ──────────────────────────────────────
            C.step(3, "Apply the policy change sequence")
            for field, value in walk:
                C.info(f"set {field} -> {value!r}")
                try:
                    result = client.set_policy({field: value}, actor="policy-walk")
                except SmartLoadError as exc:
                    rc = C.fail(f"set_policy({field}) failed: {exc}")
                    break
                if result.get("status") not in {"updated", "no-op"}:
                    rc = C.fail(f"unexpected status for {field}: {result}")
                    break
                envelope = C.wait_for_envelope(
                    pubsub,
                    POLICY_CHANNEL,
                    lambda p, _m, f=field, v=value: p.get(f) == v,
                    timeout=timeout,
                )
                if envelope is None:
                    rc = C.fail(
                        f"no PolicyUpdate with {field}={value!r} on "
                        f"{POLICY_CHANNEL} within {timeout:g}s"
                    )
                    break
                payload, _meta = envelope
                C.ok(f"{field}={payload.get(field)!r} landed "
                     f"(policy_version={payload.get('policy_version')})")
        finally:
            # ── Step 4: restore baseline ──────────────────────────────────────
            C.step(4, "Restore baseline values")
            try:
                client.set_policy(
                    {"slo_p95_latency_ms": base_slo, "safe_mode": base_safe},
                    actor="policy-walk-restore",
                )
                C.ok("baseline restored")
            except SmartLoadError as exc:
                C.warn(f"failed to restore baseline: {exc}")
            pubsub.close()

        if rc != 0:
            return rc

        # ── Step 5: show audit rows ───────────────────────────────────────────
        C.step(5, "Show recent policy audit rows")
        try:
            rows = client.audit_policy(limit=6)
        except SmartLoadError as exc:
            C.warn(f"could not read audit: {exc}")
            rows = []
        if not rows:
            C.warn("no audit rows returned")
        for row in rows:
            print(f"    - {row.get('time')} field={row.get('field')} "
                  f"new={row.get('new_value')!r} actor={row.get('actor')}")

    return C.done("policy walk complete; audit trail printed")


def main() -> int:
    parser = argparse.ArgumentParser(description="Policy change + audit demo")
    parser.add_argument("--policy-url", default=C.policy_url())
    parser.add_argument("--redis-url", default=C.redis_url())
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="seconds to wait for each PolicyUpdate (default 5)")
    args = parser.parse_args()
    return run(args.policy_url, args.redis_url, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
