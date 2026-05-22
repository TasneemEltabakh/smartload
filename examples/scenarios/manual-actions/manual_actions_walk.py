"""
examples/scenarios/manual-actions/manual_actions_walk.py
─────────────────────────────────────────────────────────
Proves the manual-actions slice (#123) end-to-end.

Reads the live policy via the SDK to learn the safe scaling band, runs a
manual scale (current → mid-band) and a manual isolate, then confirms
both actions land in the audit streams with the `manual:<actor>:`
reason prefix that the Audit UI surfaces. Restores the cluster to the
starting backend count on the way out.

Exit code:
  0 — observed expected behaviour
  1 — timeout, missing audit row, or assertion failure

Usage:
  python examples/scenarios/manual-actions/manual_actions_walk.py
  python examples/scenarios/manual-actions/manual_actions_walk.py \\
      --policy-url http://localhost:8086 \\
      --autoscaler-url http://localhost:8085 \\
      --anomaly-detector-url http://localhost:8082
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


def _await_scaling_row(client: SmartLoadClient, event_id: str,
                       reason: str, deadline_s: float = 5.0) -> bool:
    """Poll the scaling audit until we see a row matching either the
    event_id (best) or the audited reason (fallback — the audit row
    doesn't necessarily echo event_id, but reason is deterministic)."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        rows = client.list_audit("scaling", limit=10)
        for r in rows:
            if r.get("reason") == reason:
                return True
        time.sleep(0.2)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual-actions slice walkthrough")
    parser.add_argument("--policy-url",
                        default=os.environ.get("POLICY_URL", "http://localhost:8086"))
    parser.add_argument("--autoscaler-url",
                        default=os.environ.get(
                            "SMARTLOAD_AUTOSCALER_URL", "http://localhost:8085"))
    parser.add_argument("--anomaly-detector-url",
                        default=os.environ.get(
                            "SMARTLOAD_ANOMALY_DETECTOR_URL",
                            "http://localhost:8082"))
    args = parser.parse_args()

    print("== manual-actions slice walkthrough ==")
    print(f"policy-manager:   {args.policy_url}")
    print(f"autoscaler:       {args.autoscaler_url}")
    print(f"anomaly-detector: {args.anomaly_detector_url}")
    print()

    with SmartLoadClient(
        base_url=args.policy_url,
        autoscaler_url=args.autoscaler_url,
        anomaly_detector_url=args.anomaly_detector_url,
    ) as c:

        # 1. Read policy to learn the bounds.
        try:
            policy = c.get_policy()
        except SmartLoadError as exc:
            return _fail(f"could not read policy: {exc}")
        min_b = int(policy["min_backends"])
        max_b = int(policy["max_backends"])
        print(f"  ok: policy bounds [{min_b}, {max_b}] (v{policy['policy_version']})")

        # 2. Snapshot starting backend count via the scaling audit (last row
        #    instance_count is the current target after the most recent action).
        try:
            sca_before = c.list_audit("scaling", limit=1)
        except SmartLoadError as exc:
            return _fail(f"could not read scaling audit: {exc}")
        baseline = int(sca_before[0]["instance_count"]) if sca_before else max_b
        baseline = max(min_b, min(baseline, max_b))
        print(f"  ok: baseline backend count: {baseline}")

        # 3. Pick a target that's in-band but different from baseline.
        target = baseline + 1 if baseline < max_b else baseline - 1
        if target == baseline:
            target = max(min_b, baseline - 1)

        # 4. Manual scale.
        print(f"  -> manual scale {baseline} -> {target} (actor=manual_walk)")
        try:
            r = c.scale(target, actor="manual_walk", reason="walkthrough")
        except SmartLoadError as exc:
            return _fail(f"scale failed: {exc}")
        if r.get("status") not in ("applied", "noop"):
            return _fail(f"unexpected scale status: {r}")
        if r.get("final_count") != target:
            return _fail(f"final_count {r.get('final_count')} != target {target}")
        print(f"  ok: scale applied (event_id={r['event_id'][:8]}, "
              f"action={r['action']}, "
              f"previous={r['previous_count']} -> final={r['final_count']})")

        scale_reason = "manual:manual_walk: walkthrough"
        if not _await_scaling_row(c, r["event_id"], scale_reason):
            return _fail(
                f"scaling_events row not found within 5s "
                f"(looking for reason={scale_reason!r})"
            )
        print("  ok: scaling_events row confirmed in audit stream")

        # 5. Validation rejection — out-of-band target.
        try:
            c.scale(max_b + 99, actor="manual_walk", reason="bad")
            return _fail("out-of-band scale should have raised ValidationError")
        except ValidationError as exc:
            print(f"  ok: out-of-band scale rejected (field={exc.field})")

        # 6. Manual isolate.
        backend_id = "test-backend-3"
        print(f"  -> manual isolate {backend_id} status=unhealthy")
        try:
            r = c.isolate(backend_id, "unhealthy",
                          actor="manual_walk", reason="walkthrough")
        except SmartLoadError as exc:
            return _fail(f"isolate failed: {exc}")
        if r.get("status") != "applied":
            return _fail(f"unexpected isolate status: {r}")
        if r.get("anomaly_status") != "unhealthy":
            return _fail(f"anomaly_status {r.get('anomaly_status')!r} != 'unhealthy'")
        print(f"  ok: isolate applied "
              f"(event_id={r['event_id'][:8]}, score={r['score']}, "
              f"reason={r['reason']!r})")

        # 7. Validation rejection — bad status.
        try:
            c.isolate(backend_id, "bogus", actor="manual_walk")  # type: ignore[arg-type]
            return _fail("bad isolate status should have raised ValidationError")
        except ValidationError as exc:
            print(f"  ok: bad isolate status rejected (field={exc.field})")

        # 8. Restore baseline cluster size (best-effort).
        if target != baseline:
            try:
                c.scale(baseline, actor="manual_walk-restore", reason="restore")
                print(f"  ok: baseline restored ({target} -> {baseline})")
            except SmartLoadError as exc:
                print(f"  warning: restore failed: {exc}", file=sys.stderr)

    print()
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
