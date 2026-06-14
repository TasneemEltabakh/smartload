"""
examples/scenarios/forecast-autoscale/forecast_walk.py
───────────────────────────────────────────────────────
Proves the forecast-autoscale slice (docs/features/forecast-autoscale.md)
end-to-end.

Publishes a high-predicted_rps ForecastResult on smartload.forecast (the
forecasting service's channel — there is no operator-facing publish, so the
walk injects one directly to drive a deterministic prediction), then watches
smartload.scale via the SDK BFF SSE stream for the autoscaler's matching
scale_out decision and confirms it lands in the scaling audit. Finishes with
the operator override (client.scale) to show the manual half of the same
slice, then restores the starting backend count.

Exit code:
  0 — observed expected behaviour
  1 — timeout, missing audit row, or assertion failure

Usage:
  python examples/scenarios/forecast-autoscale/forecast_walk.py
  python examples/scenarios/forecast-autoscale/forecast_walk.py \\
      --policy-url http://localhost:8086 \\
      --autoscaler-url http://localhost:8085 \\
      --redis-url redis://localhost:6379
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

import redis as redis_lib

from smartload_client import SmartLoadClient, SmartLoadError

from services.shared.contracts import ForecastResult, publish_envelope

FORECAST_CHANNEL = "smartload.forecast"
SCALE_CHANNEL    = "smartload.scale"
HIGH_PREDICTED_RPS = 9999.0
SCALE_DEADLINE_SECONDS = 30.0


def _fail(msg: str) -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Forecast-autoscale slice walkthrough")
    parser.add_argument(
        "--policy-url",
        default=os.environ.get("POLICY_URL", "http://localhost:8086"),
    )
    parser.add_argument(
        "--autoscaler-url",
        default=os.environ.get("SMARTLOAD_AUTOSCALER_URL", "http://localhost:8085"),
    )
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("REDIS_URL", "redis://localhost:6379"),
    )
    args = parser.parse_args()

    print("== forecast-autoscale slice walkthrough ==")
    print(f"policy-manager: {args.policy_url}")
    print(f"autoscaler:     {args.autoscaler_url}")
    print(f"redis:          {args.redis_url}")
    print()

    rclient = redis_lib.from_url(args.redis_url)
    with SmartLoadClient(
        base_url=args.policy_url,
        autoscaler_url=args.autoscaler_url,
        redis_url=args.redis_url,
    ) as c:

        # 1. Read policy + snapshot the starting backend count.
        try:
            policy = c.get_policy()
        except SmartLoadError as exc:
            return _fail(f"could not read policy: {exc}")
        min_b, max_b = int(policy["min_backends"]), int(policy["max_backends"])
        try:
            rows = c.list_audit("scaling", limit=1)
        except SmartLoadError as exc:
            return _fail(f"could not read scaling audit: {exc}")
        baseline = int(rows[0]["instance_count"]) if rows else max_b
        baseline = max(min_b, min(baseline, max_b))
        print(f"  ok: policy bounds [{min_b}, {max_b}], baseline backends: {baseline}")

        if baseline >= max_b:
            print(f"  note: already at max_backends ({max_b}); the forecast "
                  f"cannot scale out further. Skipping the forecast leg.")
        else:
            # 2. Subscribe to smartload.scale via the BFF SSE stream.
            received: list[dict] = []
            scaled = threading.Event()

            forecast_id_holder: dict[str, str] = {}

            def on_scale(channel: str, payload: dict, _meta: dict) -> None:
                if channel != SCALE_CHANNEL:
                    return
                if payload.get("forecast_event_id") == forecast_id_holder.get("id"):
                    received.append(payload)
                    scaled.set()

            sub = c.engines.subscribe(on_scale, channels=[SCALE_CHANNEL])
            try:
                time.sleep(0.3)  # let the SSE stream attach

                # 3. Inject a high forecast.
                payload = ForecastResult(
                    horizon_minutes=5,
                    predicted_rps=HIGH_PREDICTED_RPS,
                    confidence_lower=HIGH_PREDICTED_RPS * 0.9,
                    confidence_upper=HIGH_PREDICTED_RPS * 1.1,
                    model_id="forecast-walk",
                )
                event_id = publish_envelope(
                    rclient, FORECAST_CHANNEL, source="forecast-walk", payload=payload,
                )
                forecast_id_holder["id"] = event_id
                print(f"  -> published forecast predicted_rps={HIGH_PREDICTED_RPS} "
                      f"(event_id={event_id[:8]})")

                # 4. Wait for the matching scale_out.
                if not scaled.wait(timeout=SCALE_DEADLINE_SECONDS):
                    return _fail(
                        f"no smartload.scale envelope for forecast {event_id[:8]} "
                        f"within {SCALE_DEADLINE_SECONDS}s"
                    )
                scale_payload = received[-1]
                if scale_payload.get("action") != "scale_out":
                    return _fail(f"expected scale_out, got {scale_payload.get('action')!r}")
                scaled_to = int(scale_payload["instance_count"])
                print(f"  ok: autoscaler decided scale_out -> {scaled_to} "
                      f"(forecast_event_id matched)")
            finally:
                sub.close()

            # 5. Confirm the decision is readable through the scaling audit.
            deadline = time.monotonic() + 5.0
            seen = False
            while time.monotonic() < deadline:
                for r in c.list_audit("scaling", limit=10):
                    if r.get("action") == "scale_out" and int(r.get("instance_count", -1)) == scaled_to:
                        seen = True
                        break
                if seen:
                    break
                time.sleep(0.3)
            if not seen:
                return _fail("scale_out decision not visible in scaling audit within 5s")
            print("  ok: scale_out confirmed in scaling audit stream")

        # 6. Operator override — the manual half of the same slice.
        target = baseline + 1 if baseline < max_b else baseline - 1
        target = max(min_b, min(target, max_b))
        if target != baseline:
            print(f"  -> operator override scale {baseline} -> {target}")
            try:
                r = c.scale(target, actor="forecast_walk", reason="operator override")
            except SmartLoadError as exc:
                return _fail(f"manual scale failed: {exc}")
            if r.get("status") not in ("applied", "noop"):
                return _fail(f"unexpected scale status: {r}")
            print(f"  ok: override {r['status']} (action={r['action']}, "
                  f"final={r.get('final_count')})")

        # 7. Restore the starting backend count (best-effort).
        try:
            c.scale(baseline, actor="forecast_walk-restore", reason="restore baseline")
            print(f"  ok: baseline restored ({baseline})")
        except SmartLoadError as exc:
            print(f"  warning: restore failed: {exc}", file=sys.stderr)

    rclient.close()
    print()
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
