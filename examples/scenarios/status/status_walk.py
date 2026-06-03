"""
examples/scenarios/status/status_walk.py
─────────────────────────────────────────
Proves the consolidated-status slice (#149 / OUI.9) end-to-end.

Hits `GET /api/v1/status` via the SmartLoad SDK, checks the rolled-up
`overall` pill, prints per-service status with extras, surfaces the
active policy snapshot, and shows the most recent rows from both audit
streams.

Exit code:
  0 — observed expected behaviour (response valid, overall pill makes
      sense against per-service statuses)
  1 — exception, malformed response, or invariant violation

Usage:
  python examples/scenarios/status/status_walk.py
  python examples/scenarios/status/status_walk.py \\
      --operator-ui-url http://localhost:8090

This scenario follows the SLICE_CHECKLIST template (slice #5 / #149).
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from smartload_client import SmartLoadClient, SmartLoadError, StatusResponse


def _fail(msg: str) -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


def _expected_overall(services: dict) -> str:
    """Reimplement the rollup locally so we can cross-check what the
    server returned."""
    statuses = [v.status for v in services.values()]
    if any(s == "down" for s in statuses):
        return "down"
    if any(s != "ok" for s in statuses):
        return "degraded"
    return "ok"


def _print_status(status: StatusResponse) -> None:
    pill = status.overall.upper()
    print(f"[overall] {pill}  (generated_at={status.generated_at})")
    print()
    print("[services]")
    for name, svc in sorted(status.services.items()):
        marker = {"ok": "[+]", "degraded": "[?]", "down": "[!]"}.get(svc.status, "[.]")
        extra = ", ".join(
            f"{k}={v}" for k, v in svc.extra.items() if k != "error"
        ) or "—"
        if svc.status != "ok" and svc.extra.get("error"):
            extra = f"error={svc.extra['error']}"
        print(f"  {marker} {name:18s} {svc.status:9s}  {extra}")
    print()
    if status.active_policy is not None:
        ap = status.active_policy
        print(
            f"[active_policy] mode={ap.operating_mode}  "
            f"safe_mode={ap.safe_mode}  "
            f"slo_p95_ms={ap.slo_p95_latency_ms}  "
            f"version={ap.policy_version}"
        )
    else:
        print("[active_policy] (unavailable)")
    print()
    if status.recent.last_policy_change:
        row = status.recent.last_policy_change
        print(
            f"[last_policy_change] {row.get('at')}  "
            f"actor={row.get('actor')}  "
            f"field={row.get('field')}  "
            f"{row.get('from')!r} -> {row.get('to')!r}"
        )
    else:
        print("[last_policy_change] (none)")
    if status.recent.last_scaling_event:
        row = status.recent.last_scaling_event
        print(
            f"[last_scaling_event] {row.get('at')}  "
            f"action={row.get('action')}  "
            f"count={row.get('instance_count')}  "
            f"reason={row.get('reason')!r}"
        )
    else:
        print("[last_scaling_event] (none)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Consolidated-status slice walkthrough")
    parser.add_argument(
        "--operator-ui-url",
        default=os.environ.get("SMARTLOAD_OPERATOR_UI_URL", "http://localhost:8090"),
        help="Operator-UI BFF base URL (default: http://localhost:8090)",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Number of consecutive status reads (default 1; >1 verifies timing stability)",
    )
    args = parser.parse_args()

    print(f"# operator-ui-url: {args.operator_ui_url}")
    print()

    with SmartLoadClient(operator_ui_url=args.operator_ui_url) as client:

        for i in range(args.repeat):
            print(f"### read {i + 1}/{args.repeat}")
            try:
                t0 = time.monotonic()
                status = client.get_status()
                elapsed = time.monotonic() - t0
            except SmartLoadError as exc:
                return _fail(f"get_status() raised: {exc}")

            print(f"# elapsed: {elapsed * 1000:.0f} ms")
            print()

            if not isinstance(status, StatusResponse):
                return _fail(f"expected StatusResponse, got {type(status).__name__}")
            if not status.services:
                return _fail("services map is empty — BFF has no SERVICE_URLS configured?")
            if status.overall not in {"ok", "degraded", "down"}:
                return _fail(f"unexpected overall pill: {status.overall!r}")

            expected = _expected_overall(status.services)
            if expected != status.overall:
                return _fail(
                    f"overall mismatch: server says {status.overall!r} but "
                    f"per-service rollup yields {expected!r}"
                )

            _print_status(status)

            # Latency sanity — even with one service hanging, total should be
            # within ~3s. Fan-out is parallel + capped at 2s per service. We
            # allow a generous margin (5s) so transient slow environments
            # don't fail the scenario.
            if elapsed > 5.0:
                return _fail(f"response took {elapsed:.2f} s (budget 5 s)")

            print()
            if i < args.repeat - 1:
                time.sleep(0.5)

    print("OK - /api/v1/status round-trip clean, rollup matches per-service map, latency within budget.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
