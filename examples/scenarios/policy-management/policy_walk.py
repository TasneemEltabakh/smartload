"""
examples/scenarios/policy-management/policy_walk.py
────────────────────────────────────────────────────
Proves the policy-management slice end-to-end.

Reads the current policy via the SmartLoad SDK, toggles a single field,
watches smartload.policy for the matching envelope, restores baseline,
and prints the most recent audit rows.

Exit code:
  0 — observed expected behaviour
  1 — timeout or assertion failure

Usage:
  python examples/scenarios/policy-management/policy_walk.py
  python examples/scenarios/policy-management/policy_walk.py \\
      --policy-url http://localhost:8086 --redis-url redis://localhost:6379
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from typing import Any

from smartload_client import SmartLoadClient, SmartLoadError


def _fail(msg: str) -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Policy management slice walkthrough")
    parser.add_argument(
        "--policy-url",
        default=os.environ.get("POLICY_URL", "http://localhost:8086"),
    )
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("REDIS_URL", "redis://localhost:6379"),
    )
    args = parser.parse_args()

    print("== policy-management slice walkthrough ==")
    print(f"policy-manager: {args.policy_url}")
    print(f"redis:          {args.redis_url}")
    print()

    with SmartLoadClient(base_url=args.policy_url, redis_url=args.redis_url) as c:
        # 1. Snapshot baseline.
        try:
            baseline = c.get_policy()
        except SmartLoadError as exc:
            return _fail(f"could not read policy: {exc}")
        baseline_safe_mode = bool(baseline.get("safe_mode", False))
        print(f"  ok: baseline read (safe_mode={baseline_safe_mode}, "
              f"policy_version={baseline.get('policy_version')})")

        # 2. Subscribe to smartload.policy in a background thread.
        received: list[dict[str, Any]] = []
        envelope_arrived = threading.Event()

        def on_update(payload: dict, _meta: dict) -> None:
            received.append(payload)
            envelope_arrived.set()

        sub = c.subscribe_policy(on_update)
        try:
            # Brief settle to drain any pre-attach backlog.
            time.sleep(0.3)
            envelope_arrived.clear()
            received.clear()

            # 3. Toggle safe_mode.
            target = not baseline_safe_mode
            print(f"  -> toggling safe_mode {baseline_safe_mode} -> {target}")
            try:
                result = c.set_policy({"safe_mode": target}, actor="policy_walk")
            except SmartLoadError as exc:
                return _fail(f"set_policy failed: {exc}")

            if result.get("status") not in {"updated", "no-op"}:
                return _fail(f"unexpected status: {result}")

            # 4. Wait for the matching envelope.
            if not envelope_arrived.wait(timeout=5.0):
                return _fail("no smartload.policy envelope arrived within 5s")
            last = received[-1]
            if bool(last.get("safe_mode")) is not target:
                return _fail(f"envelope safe_mode != target ({last.get('safe_mode')} vs {target})")
            print(f"  ok: envelope received (policy_version={last.get('policy_version')}, "
                  f"changed_fields={last.get('changed_fields')})")

            # 5. Restore baseline.
            try:
                c.set_policy({"safe_mode": baseline_safe_mode}, actor="policy_walk-restore")
            except SmartLoadError as exc:
                print(f"  warning: failed to restore baseline: {exc}", file=sys.stderr)

            # 6. Print recent audit rows.
            try:
                rows = c.audit_policy(limit=5)
            except SmartLoadError as exc:
                print(f"  warning: could not read audit: {exc}", file=sys.stderr)
                rows = []
            print(f"  audit (latest {len(rows)} rows):")
            for row in rows:
                print(f"    - {row.get('time')} field={row.get('field')} "
                      f"new={row.get('new_value')!r} actor={row.get('actor')}")
        finally:
            sub.close()

    print()
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
