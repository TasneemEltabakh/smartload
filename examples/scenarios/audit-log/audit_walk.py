"""
examples/scenarios/audit-log/audit_walk.py
───────────────────────────────────────────
Proves the audit-log slice (#122) end-to-end.

Reads both audit streams via the SmartLoad SDK, triggers a fresh policy
change, confirms the matching row arrives in the policy audit stream,
prints recent rows from both streams, and demonstrates the kind-dispatch
+ limit cap behaviour.

Exit code:
  0 — observed expected behaviour
  1 — timeout, missing row, or assertion failure

Usage:
  python examples/scenarios/audit-log/audit_walk.py
  python examples/scenarios/audit-log/audit_walk.py \\
      --policy-url http://localhost:8086 \\
      --autoscaler-url http://localhost:8085
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from smartload_client import SmartLoadClient, SmartLoadError, ValidationError


def _fail(msg: str) -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


def _print_rows(label: str, rows: list[dict], take: int) -> None:
    print(f"  {label} (latest {min(len(rows), take)} of {len(rows)}):")
    for row in rows[:take]:
        if "field" in row:
            print(f"    - {row.get('time')} field={row.get('field')} "
                  f"new={row.get('new_value')!r} actor={row.get('actor')}")
        else:
            print(f"    - {row.get('time')} action={row.get('action')} "
                  f"count={row.get('instance_count')} reason={row.get('reason')!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit-log slice walkthrough")
    parser.add_argument(
        "--policy-url",
        default=os.environ.get("POLICY_URL", "http://localhost:8086"),
    )
    parser.add_argument(
        "--autoscaler-url",
        default=os.environ.get(
            "SMARTLOAD_AUTOSCALER_URL",
            os.environ.get("AUTOSCALER_URL", "http://localhost:8085"),
        ),
    )
    args = parser.parse_args()

    print("== audit-log slice walkthrough ==")
    print(f"policy-manager: {args.policy_url}")
    print(f"autoscaler:     {args.autoscaler_url}")
    print()

    with SmartLoadClient(base_url=args.policy_url,
                         autoscaler_url=args.autoscaler_url) as c:

        # 1. Snapshot baseline so we can restore it.
        try:
            baseline = c.get_policy()
        except SmartLoadError as exc:
            return _fail(f"could not read baseline policy: {exc}")
        baseline_safe_mode = bool(baseline.get("safe_mode", False))
        print(f"  ok: baseline read "
              f"(safe_mode={baseline_safe_mode}, "
              f"policy_version={baseline.get('policy_version')})")

        # 2. Read both audit streams via the unified SDK surface.
        try:
            pol_before = c.list_audit("policy", limit=5)
            sca_before = c.list_audit("scaling", limit=5)
        except SmartLoadError as exc:
            return _fail(f"could not read audit streams: {exc}")
        print(f"  ok: policy_changes rows visible: {len(pol_before)}")
        print(f"  ok: scaling_events rows visible: {len(sca_before)}")

        # 3. Trigger a fresh policy change and confirm the row lands.
        target = not baseline_safe_mode
        print(f"  -> toggling safe_mode {baseline_safe_mode} -> {target} "
              f"(actor=audit_walk)")
        try:
            result = c.set_policy({"safe_mode": target}, actor="audit_walk")
        except SmartLoadError as exc:
            return _fail(f"set_policy failed: {exc}")
        if result.get("status") not in {"updated", "no-op"}:
            return _fail(f"unexpected status: {result}")

        # Audit write is synchronous in the policy-manager but give it a
        # brief settle for the DB commit.
        time.sleep(0.5)

        try:
            pol_after = c.list_audit("policy", limit=10)
        except SmartLoadError as exc:
            return _fail(f"could not re-read policy audit: {exc}")

        matching = [
            r for r in pol_after
            if r.get("field") == "safe_mode"
            and r.get("actor") == "audit_walk"
            and bool(r.get("new_value")) is target
        ]
        if not matching:
            return _fail(
                "no matching audit row found after policy change "
                f"(searched {len(pol_after)} latest rows)"
            )
        latest = matching[0]
        print(f"  ok: audit row confirmed "
              f"(policy_version={latest.get('policy_version')}, "
              f"time={latest.get('time')})")

        # 4. Restore baseline (best-effort; never crash the demo on failure).
        try:
            c.set_policy({"safe_mode": baseline_safe_mode},
                         actor="audit_walk-restore")
            print(f"  ok: baseline restored (safe_mode={baseline_safe_mode})")
        except SmartLoadError as exc:
            print(f"  warning: failed to restore baseline: {exc}",
                  file=sys.stderr)

        # 5. Show recent rows from both streams.
        try:
            pol_rows = c.list_audit("policy", limit=5)
            sca_rows = c.list_audit("scaling", limit=5)
        except SmartLoadError as exc:
            return _fail(f"could not read final audit snapshot: {exc}")

        _print_rows("policy_changes", pol_rows, take=5)
        _print_rows("scaling_events", sca_rows, take=5)

        # 6. Demonstrate the limit cap.
        capped = c.list_audit("policy", limit=2)
        if len(capped) > 2:
            return _fail(f"limit=2 returned {len(capped)} rows (expected <=2)")
        print(f"  ok: limit=2 returned {len(capped)} rows (cap respected)")

        # 7. Demonstrate validation on the kind dispatch.
        try:
            c.list_audit("invalid-kind", limit=1)  # type: ignore[arg-type]
            return _fail("unknown kind should have raised ValidationError")
        except ValidationError as exc:
            print(f"  ok: invalid kind rejected ({exc})")

    print()
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
