"""
Smoke-run artifact capture for T2.1.
Run from the smartload project root after `docker compose up` is healthy.

Usage:
    python experiments/lb-sidecar/<date>-smoke/capture.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent.parent.parent  # smartload/

COMPOSE_PS_FILE = HERE / "compose_ps.txt"
LB_SIDECAR_LOG = HERE / "lb_sidecar.log"
RL_ENGINE_LOG = HERE / "rl_engine.log"
SHADOW_ENVELOPE_FILE = HERE / "shadow_envelope.json"
UPSTREAM_CONF_FILE = HERE / "upstream_conf_shadow.txt"

CONTAINER_LB = "smartload-lb-sidecar-1"
CONTAINER_LB_ALT = "smartload-lb-sidecar-1"


def run(cmd: list[str], **kw) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return (r.stdout + r.stderr).strip()


def main() -> int:
    os.chdir(ROOT)
    print(f"Saving artifacts to: {HERE}")

    # 1. compose ps
    ps = run(["docker", "compose", "ps"])
    COMPOSE_PS_FILE.write_text(ps)
    print(f"  [OK] compose_ps.txt ({len(ps.splitlines())} lines)")

    # 2. lb-sidecar logs
    lbl = run(["docker", "compose", "logs", "--tail=200", "lb-sidecar"])
    LB_SIDECAR_LOG.write_text(lbl)
    print(f"  [OK] lb_sidecar.log ({len(lbl.splitlines())} lines)")

    # 3. rl-engine logs
    rll = run(["docker", "compose", "logs", "--tail=200", "rl-engine"])
    RL_ENGINE_LOG.write_text(rll)
    print(f"  [OK] rl_engine.log ({len(rll.splitlines())} lines)")

    # 4. Capture one shadow envelope from Redis
    try:
        import redis as redis_lib
    except ImportError:
        print("  SKIP shadow_envelope.json (redis-py not installed)")
    else:
        r = redis_lib.from_url("redis://localhost:6379", decode_responses=True)
        p = r.pubsub()
        p.subscribe("smartload.routing")
        print("  Subscribed to smartload.routing — waiting up to 15s for a shadow envelope...")
        deadline = time.monotonic() + 15.0
        captured = None
        while time.monotonic() < deadline:
            msg = p.get_message(timeout=1.0)
            if msg and msg["type"] == "message":
                try:
                    data = json.loads(msg["data"])
                    if data.get("payload", {}).get("mode") == "shadow":
                        captured = data
                        break
                    else:
                        print(f"    (skipped non-shadow envelope: mode={data.get('payload', {}).get('mode')})")
                except Exception:
                    pass
        p.unsubscribe()
        if captured:
            SHADOW_ENVELOPE_FILE.write_text(json.dumps(captured, indent=2))
            print(f"  [OK] shadow_envelope.json — mode={captured['payload']['mode']}")
        else:
            print("  WARN: no shadow envelope received within 15s")

    # 5. upstream.conf snapshot
    conf = run(["docker", "exec", CONTAINER_LB, "cat", "/nginx-conf/upstream.conf"])
    if "Error" in conf or "No such" in conf:
        conf = run(["docker", "exec", CONTAINER_LB_ALT, "cat", "/nginx-conf/upstream.conf"])
    UPSTREAM_CONF_FILE.write_text(conf)
    print(f"  [OK] upstream_conf_shadow.txt ({len(conf.splitlines())} lines)")

    # 6. Verify weights are all = 1 (shadow gate held)
    weight_lines = [l for l in conf.splitlines() if "weight=" in l]
    non_one = [l for l in weight_lines if "weight=1 " not in l]
    if non_one:
        print(f"  FAIL: upstream.conf has non-equal weights after shadow run:")
        for l in non_one:
            print(f"    {l.strip()}")
        return 1
    else:
        print(f"  [OK] shadow gate confirmed: all {len(weight_lines)} backends at weight=1")

    print(f"\nAll artifacts saved to {HERE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
