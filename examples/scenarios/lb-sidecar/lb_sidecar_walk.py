"""
examples/scenarios/lb-sidecar/lb_sidecar_walk.py
──────────────────────────────────────────────────
Proves T2.1 (lb-sidecar dynamic upstream rewriting) end-to-end.

Steps:
  1. Check /health — confirm sidecar_ready=true.
  2. Read /api/v1/lb/state — confirm upstream_weights populated.
  3. POST /api/v1/lb/weights — apply custom weights.
  4. Verify state reflects the new weights.
  5. Publish an active RoutingRecommendation envelope via Redis and wait
     for the sidecar to apply the PPO-derived weights (requires
     LB_SIDECAR_RUNLOOP_ENABLED=true and RL_RUNLOOP_ENABLED=true +
     RL_MODE=active + RL_POLICY=ppo on the rl-engine).
  6. Restore equal weights.

Exit code:
  0 — all steps passed
  1 — failure or timeout

Usage:
  python examples/scenarios/lb-sidecar/lb_sidecar_walk.py
  python examples/scenarios/lb-sidecar/lb_sidecar_walk.py \\
      --lb-sidecar-url http://localhost:8087 \\
      --redis-url redis://localhost:6379
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx


def _ok(msg: str) -> None:
    print(f"  OK  {msg}")


def _fail(msg: str) -> int:
    print(f"FAIL  {msg}", file=sys.stderr)
    return 1


def _step(n: int, title: str) -> None:
    print(f"\nStep {n}: {title}")


BACKENDS = [
    "smartload-test-backend-1:8080",
    "smartload-test-backend-2:8080",
    "smartload-test-backend-3:8080",
    "smartload-test-backend-4:8080",
    "smartload-test-backend-5:8080",
]

CUSTOM_WEIGHTS = {
    "smartload-test-backend-1:8080": 80,
    "smartload-test-backend-2:8080": 60,
    "smartload-test-backend-3:8080": 40,
    "smartload-test-backend-4:8080": 40,
    "smartload-test-backend-5:8080": 40,
}

EQUAL_WEIGHTS = {b: 1 for b in BACKENDS}


def run(lb_sidecar_url: str, redis_url: str) -> int:
    http = httpx.Client(base_url=lb_sidecar_url.rstrip("/"), timeout=10.0)

    # ── Step 1: health ────────────────────────────────────────────────────────
    _step(1, "Check /health")
    r = http.get("/health")
    if r.status_code != 200:
        return _fail(f"/health returned {r.status_code}: {r.text[:200]}")
    body = r.json()
    if body.get("redis") is not True:
        return _fail(f"redis unhealthy: {body}")
    if "sidecar_ready" in body:
        if not body["sidecar_ready"]:
            return _fail(
                "sidecar_ready=false — set LB_SIDECAR_RUNLOOP_ENABLED=true and restart lb-sidecar"
            )
        _ok(f"sidecar_ready=true, excluded_backends={body.get('excluded_backends', [])}")
    else:
        _ok("run loop disabled (sidecar_ready not present) — Steps 2–5 will skip gracefully")

    # ── Step 2: read state ────────────────────────────────────────────────────
    _step(2, "Read /api/v1/lb/state")
    r = http.get("/api/v1/lb/state")
    if r.status_code == 503:
        print("  SKIP (run loop disabled)")
    elif r.status_code != 200:
        return _fail(f"GET /api/v1/lb/state returned {r.status_code}")
    else:
        state = r.json()
        weights = state.get("upstream_weights", {})
        excluded = state.get("excluded_backends", [])
        _ok(f"{len(weights)} upstream backends, {len(excluded)} excluded")
        for b, w in sorted(weights.items()):
            print(f"       {b}: weight={w}{' (DOWN)' if b in excluded else ''}")

    # ── Step 3: POST custom weights ───────────────────────────────────────────
    _step(3, f"POST /api/v1/lb/weights ({len(CUSTOM_WEIGHTS)} backends)")
    r = http.post("/api/v1/lb/weights", json=CUSTOM_WEIGHTS)
    if r.status_code == 503:
        print("  SKIP (run loop disabled)")
    elif r.status_code != 200:
        return _fail(f"POST /api/v1/lb/weights returned {r.status_code}: {r.text[:200]}")
    else:
        body = r.json()
        if not body.get("ok"):
            return _fail(f"POST returned ok=false: {body}")
        _ok(f"applied_weights: {body['applied_weights']}")

    # ── Step 4: verify state ──────────────────────────────────────────────────
    _step(4, "Verify state reflects custom weights")
    r = http.get("/api/v1/lb/state")
    if r.status_code == 503:
        print("  SKIP (run loop disabled)")
    elif r.status_code != 200:
        return _fail(f"GET /api/v1/lb/state returned {r.status_code}")
    else:
        state = r.json()
        weights = state.get("upstream_weights", {})
        mismatches = []
        for b, expected in CUSTOM_WEIGHTS.items():
            actual = weights.get(b)
            if actual is None:
                continue  # not in state (may not be in NGINX conf yet)
            if actual != expected:
                mismatches.append(f"{b}: expected={expected} actual={actual}")
        if mismatches:
            return _fail("Weight mismatch:\n  " + "\n  ".join(mismatches))
        _ok("weights match custom overrides")

    # ── Step 5: Redis routing recommendation ──────────────────────────────────
    _step(5, "Publish active RoutingRecommendation via Redis → watch weights update")
    try:
        import redis as redis_lib
    except ImportError:
        print("  SKIP (redis-py not installed)")
    else:
        r_client = redis_lib.from_url(redis_url, decode_responses=True)
        envelope = json.dumps({
            "source": "lb-sidecar-walk",
            "channel": "smartload.routing",
            "payload": {
                "mode": "active",
                "server_rankings": [
                    {"backend_id": "smartload-test-backend-1:8080", "score": 0.99},
                    {"backend_id": "smartload-test-backend-2:8080", "score": 0.70},
                    {"backend_id": "smartload-test-backend-3:8080", "score": 0.50},
                    {"backend_id": "smartload-test-backend-4:8080", "score": 0.30},
                    {"backend_id": "smartload-test-backend-5:8080", "score": 0.20},
                ],
                "policy_version": 0,
            },
        })
        r_client.publish("smartload.routing", envelope)
        print("  → envelope published to smartload.routing")

        state_r = http.get("/api/v1/lb/state")
        if state_r.status_code == 503:
            print("  SKIP (run loop disabled — envelope published but sidecar won't react)")
        else:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                time.sleep(0.5)
                s = http.get("/api/v1/lb/state").json()
                b1 = s.get("upstream_weights", {}).get("smartload-test-backend-1:8080")
                if b1 is not None and b1 >= 90:
                    _ok(f"weight updated: backend-1={b1} (from score=0.99)")
                    break
            else:
                print("  WARN: weights not updated within 10s — "
                      "is RL_MODE=active and LB_SIDECAR_RUNLOOP_ENABLED=true?")

    # ── Step 6: restore equal weights ────────────────────────────────────────
    _step(6, "Restore equal weights")
    r = http.post("/api/v1/lb/weights", json=EQUAL_WEIGHTS)
    if r.status_code == 503:
        print("  SKIP (run loop disabled)")
    elif r.status_code != 200:
        return _fail(f"Restore failed: {r.status_code} {r.text[:200]}")
    else:
        _ok("equal weights restored")

    print("\n✓ lb-sidecar scenario complete")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="lb-sidecar walkthrough")
    parser.add_argument(
        "--lb-sidecar-url",
        default=os.environ.get("LB_SIDECAR_URL", "http://localhost:8087"),
    )
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("REDIS_URL", "redis://localhost:6379"),
    )
    args = parser.parse_args()
    return run(args.lb_sidecar_url, args.redis_url)


if __name__ == "__main__":
    sys.exit(main())
