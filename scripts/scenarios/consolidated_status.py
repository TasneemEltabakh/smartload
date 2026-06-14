#!/usr/bin/env python3
"""
scripts/scenarios/consolidated_status.py
─────────────────────────────────────────
DEMO: prove the consolidated status read. Hits GET /api/v1/status on the
operator-UI BFF and prints the rolled-up health pill, per-service detail, the
active policy snapshot, and the most recent audit rows from both streams.

What it does:
  1. Call GET /api/v1/status via the SmartLoad SDK.
  2. Print the overall pill + per-service status lines.
  3. Print the active policy snapshot.
  4. Print the most recent policy change + scaling event.
  5. Cross-check the server's overall pill against a local rollup.

This narrates to the console; it is not a pytest test. Exit 0 when the read is
well-formed and the rollup is consistent, 1 otherwise.

Usage:
  python scripts/scenarios/consolidated_status.py
  python scripts/scenarios/consolidated_status.py \\
      --operator-ui-url http://localhost:8090
"""

from __future__ import annotations

import argparse
import sys
import time

import _common as C


def _expected_overall(services: dict) -> str:
    """Local rollup so we can cross-check the server's `overall` pill."""
    statuses = [svc.status for svc in services.values()]
    if any(s == "down" for s in statuses):
        return "down"
    if any(s != "ok" for s in statuses):
        return "degraded"
    return "ok"


def run(operator_ui_url: str) -> int:
    from smartload_client import SmartLoadClient, SmartLoadError, StatusResponse

    C.banner(
        "consolidated status demo",
        "Read GET /api/v1/status and show the rolled-up health, per-service "
        "detail, active policy, and recent audit rows.",
    )
    C.conn("operator-ui:", operator_ui_url)

    with SmartLoadClient(operator_ui_url=operator_ui_url) as client:
        # ── Step 1: read status ───────────────────────────────────────────────
        C.step(1, "GET /api/v1/status")
        try:
            t0 = time.monotonic()
            status = client.get_status()
            elapsed_ms = (time.monotonic() - t0) * 1000.0
        except SmartLoadError as exc:
            return C.fail(f"get_status() raised: {exc}")
        if not isinstance(status, StatusResponse):
            return C.fail(f"expected StatusResponse, got {type(status).__name__}")
        if not status.services:
            return C.fail("services map is empty -- BFF has no service URLs configured?")
        C.ok(f"overall={status.overall.upper()} services={len(status.services)} "
             f"({elapsed_ms:.0f} ms)")

        # ── Step 2: per-service detail ────────────────────────────────────────
        C.step(2, "Per-service status")
        for name, svc in sorted(status.services.items()):
            marker = {"ok": "[+]", "degraded": "[?]", "down": "[!]"}.get(svc.status, "[.]")
            extra = ", ".join(
                f"{k}={v}" for k, v in svc.extra.items() if k != "error"
            ) or "-"
            if svc.status != "ok" and svc.extra.get("error"):
                extra = f"error={svc.extra['error']}"
            print(f"    {marker} {name:18s} {svc.status:9s} {extra}")

        # ── Step 3: active policy ─────────────────────────────────────────────
        C.step(3, "Active policy snapshot")
        if status.active_policy is not None:
            ap = status.active_policy
            C.ok(f"mode={ap.operating_mode} safe_mode={ap.safe_mode} "
                 f"slo_p95_ms={ap.slo_p95_latency_ms} version={ap.policy_version}")
        else:
            C.warn("active policy unavailable")

        # ── Step 4: recent audit rows ─────────────────────────────────────────
        C.step(4, "Most recent audit rows")
        if status.recent.last_policy_change:
            row = status.recent.last_policy_change
            C.ok(f"last policy change: {row.get('at')} actor={row.get('actor')} "
                 f"field={row.get('field')} {row.get('from')!r} -> {row.get('to')!r}")
        else:
            C.info("no recent policy change")
        if status.recent.last_scaling_event:
            row = status.recent.last_scaling_event
            C.ok(f"last scaling event: {row.get('at')} action={row.get('action')} "
                 f"count={row.get('instance_count')} reason={row.get('reason')!r}")
        else:
            C.info("no recent scaling event")

        # ── Step 5: rollup cross-check ────────────────────────────────────────
        C.step(5, "Cross-check the overall pill")
        if status.overall not in {"ok", "degraded", "down"}:
            return C.fail(f"unexpected overall pill: {status.overall!r}")
        expected = _expected_overall(status.services)
        if expected != status.overall:
            return C.fail(
                f"overall mismatch: server says {status.overall!r} but the "
                f"per-service rollup yields {expected!r}"
            )
        C.ok(f"server overall {status.overall!r} matches local rollup")

    return C.done("consolidated status read clean")


def main() -> int:
    parser = argparse.ArgumentParser(description="Consolidated status demo")
    parser.add_argument("--operator-ui-url", default=C.operator_ui_url())
    args = parser.parse_args()
    return run(args.operator_ui_url)


if __name__ == "__main__":
    sys.exit(main())
