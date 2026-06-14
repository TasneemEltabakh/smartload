#!/usr/bin/env python3
"""
scripts/scenarios/safe_mode_toggle.py
──────────────────────────────────────
DEMO: prove safe-mode propagation. Toggles safe_mode via policy-manager, watches
smartload.policy for the snapshot, confirms the new policy_version reaches the
autoscaler, then restores the original flag.

What it does:
  1. Snapshot the current policy (safe_mode + policy_version) via the SDK.
  2. Subscribe to smartload.policy.
  3. POST safe_mode=<flipped> to policy-manager.
  4. Watch smartload.policy for a PolicyUpdate carrying the flipped flag.
  5. Confirm the autoscaler /health reports the bumped policy_version.
  6. POST safe_mode=<original> to restore baseline.

This narrates to the console; it is not a pytest test. Exit 0 on observed
propagation, 1 on timeout or mismatch.

Usage:
  python scripts/scenarios/safe_mode_toggle.py
  python scripts/scenarios/safe_mode_toggle.py --timeout 5
  python scripts/scenarios/safe_mode_toggle.py \\
      --policy-url http://localhost:8086 \\
      --autoscaler-url http://localhost:8085 \\
      --redis-url redis://localhost:6379
"""

from __future__ import annotations

import argparse
import sys
import time

import _common as C


def run(policy_url: str, autoscaler_url: str, redis_url: str,
        timeout: float) -> int:
    from smartload_client import SmartLoadClient, SmartLoadError

    POLICY_CHANNEL = "smartload.policy"

    C.banner(
        "safe-mode toggle demo",
        f"Flip safe_mode; expect a PolicyUpdate on {POLICY_CHANNEL} within "
        f"{timeout:g}s and the autoscaler to pick up the new policy_version.",
    )
    C.conn("policy-manager:", policy_url)
    C.conn("autoscaler:", autoscaler_url)
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
        baseline_safe = bool(baseline.get("safe_mode", False))
        baseline_version = baseline.get("policy_version")
        C.ok(f"safe_mode={baseline_safe} policy_version={baseline_version}")

        # ── Step 2: subscribe ─────────────────────────────────────────────────
        C.step(2, f"Subscribe to {POLICY_CHANNEL}")
        pubsub = C.subscribe(redis_client, POLICY_CHANNEL)
        C.ok("subscribed")

        target = not baseline_safe
        try:
            # ── Step 3: flip safe_mode ────────────────────────────────────────
            C.step(3, f"POST safe_mode {baseline_safe} -> {target}")
            try:
                result = client.set_policy({"safe_mode": target}, actor="safe-mode-toggle")
            except SmartLoadError as exc:
                return C.fail(f"set_policy failed: {exc}")
            if result.get("status") not in {"updated", "no-op"}:
                return C.fail(f"unexpected set_policy status: {result}")
            new_version = result.get("policy_version")
            C.ok(f"policy updated (status={result.get('status')} "
                 f"policy_version={new_version})")

            # ── Step 4: watch smartload.policy ────────────────────────────────
            C.step(4, f"Watch {POLICY_CHANNEL} for the flipped flag")
            envelope = C.wait_for_envelope(
                pubsub,
                POLICY_CHANNEL,
                lambda p, _m: bool(p.get("safe_mode")) is target,
                timeout=timeout,
            )
            if envelope is None:
                return C.fail(
                    f"no PolicyUpdate with safe_mode={target} on "
                    f"{POLICY_CHANNEL} within {timeout:g}s"
                )
            payload, _meta = envelope
            C.ok(f"PolicyUpdate observed: safe_mode={payload.get('safe_mode')} "
                 f"policy_version={payload.get('policy_version')} "
                 f"changed_fields={payload.get('changed_fields')}")

            # ── Step 5: confirm autoscaler picked up the bump ─────────────────
            C.step(5, "Confirm autoscaler /health reflects the new policy_version")
            seen = _await_autoscaler_version(autoscaler_url, payload.get("policy_version"),
                                             deadline_s=timeout)
            if seen is None:
                C.warn("could not confirm autoscaler policy_version within deadline")
            else:
                C.ok(f"autoscaler policy_version={seen}")
        finally:
            # ── Step 6: restore baseline ──────────────────────────────────────
            C.step(6, f"Restore safe_mode -> {baseline_safe}")
            try:
                client.set_policy({"safe_mode": baseline_safe}, actor="safe-mode-toggle-restore")
                C.ok("baseline restored")
            except SmartLoadError as exc:
                C.warn(f"failed to restore baseline safe_mode: {exc}")
            pubsub.close()

    return C.done("safe-mode toggled, propagated, and restored")


def _await_autoscaler_version(autoscaler_url: str, expected_version,
                              deadline_s: float) -> int | None:
    """Poll the autoscaler /health until its policy_version reaches at least
    `expected_version`, or the deadline lapses. Returns the observed version or
    None on timeout / unreachable."""
    if expected_version is None:
        expected_version = 0
    deadline = time.monotonic() + deadline_s
    last = None
    while time.monotonic() < deadline:
        code, body = C.http_get_json(f"{autoscaler_url.rstrip('/')}/health")
        if code in (200, 503) and isinstance(body, dict):
            last = body.get("policy_version")
            try:
                if last is not None and int(last) >= int(expected_version):
                    return last
            except (TypeError, ValueError):
                return last
        time.sleep(0.5)
    return last if last is not None else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe-mode toggle demo")
    parser.add_argument("--policy-url", default=C.policy_url())
    parser.add_argument("--autoscaler-url", default=C.autoscaler_url())
    parser.add_argument("--redis-url", default=C.redis_url())
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="seconds to wait for propagation (default 5)")
    args = parser.parse_args()
    return run(args.policy_url, args.autoscaler_url, args.redis_url, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
