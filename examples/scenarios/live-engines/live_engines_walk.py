"""
examples/scenarios/live-engines/live_engines_walk.py
─────────────────────────────────────────────────────
Proves the Live Engines slice (#121) end-to-end without going through
the browser.

Three steps:
  1. Hit the per-engine /api/v1/engine/state endpoint on each of the
     three AI services (anomaly-detector, forecasting, rl-engine).
     Assert the canonical response shape (engine block, stats, etc).
  2. Hit the BFF's /api/ui/engines/snapshot endpoint. Assert that the
     parallel fan-out covers all three services + the four per-channel
     ring snapshots.
  3. Open an SSE connection to /api/ui/engines/stream, read the replay
     burst, observe at least one live event within a generous window.

Exit code:
  0 — observed expected behaviour
  1 — assertion failure or timeout

Usage:
  python examples/scenarios/live-engines/live_engines_walk.py
  python examples/scenarios/live-engines/live_engines_walk.py \\
      --operator-ui-url http://localhost:8090 \\
      --stream-wait-seconds 20

Requires: only the Python stdlib. The SDK methods for Live Engines are
pending — see docs/features/live-engines.md status.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


REQUIRED_ENGINE_STATE_KEYS = {"service", "channel", "runloop_enabled", "engine", "stats"}
REQUIRED_SNAPSHOT_TOP_KEYS = {"services", "channels"}
EXPECTED_SNAPSHOT_SERVICES = {"anomaly-detector", "forecasting", "rl-engine"}
EXPECTED_SNAPSHOT_CHANNELS = {"smartload.anomaly", "smartload.forecast", "smartload.routing", "smartload.scale"}


def _fail(msg: str) -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


def _get_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def _check_engine_state(svc_name: str, url: str) -> bool:
    try:
        body = _get_json(url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"  {svc_name:18s} UNREACHABLE  ({exc})")
        return False

    missing = REQUIRED_ENGINE_STATE_KEYS - set(body)
    if missing:
        print(f"  {svc_name:18s} BAD SHAPE    missing {sorted(missing)}")
        return False

    engine = body.get("engine") or {}
    stats = body.get("stats") or {}
    print(f"  {svc_name:18s} OK  loaded={engine.get('loaded')!s:18s} "
          f"ticks={stats.get('ticks_total')} publishes={stats.get('publishes_total')}")
    return True


def _check_snapshot(snapshot: dict) -> bool:
    missing_top = REQUIRED_SNAPSHOT_TOP_KEYS - set(snapshot)
    if missing_top:
        _fail(f"snapshot missing top-level keys: {sorted(missing_top)}")
        return False

    services_seen = set(snapshot["services"])
    services_missing = EXPECTED_SNAPSHOT_SERVICES - services_seen
    if services_missing:
        _fail(f"snapshot missing AI services: {sorted(services_missing)}")
        return False

    channels_seen = set(snapshot["channels"])
    channels_missing = EXPECTED_SNAPSHOT_CHANNELS - channels_seen
    if channels_missing:
        _fail(f"snapshot missing channel rings: {sorted(channels_missing)}")
        return False

    print(f"  fan-out covers {len(services_seen)} services + {len(channels_seen)} channel rings")
    return True


def _read_one_sse_event(url: str, stream_wait_seconds: float) -> bool:
    """Open the SSE stream and return True as soon as one `data:` line
    arrives. Heartbeat comments (lines starting with `:`) are ignored.

    The BFF emits either explicit `event: <channel>\\ndata: <json>\\n\\n`
    frames during replay or just `data: <json>\\n\\n` frames during live
    fan-out, so we only require the `data:` line to confirm the stream
    is alive.
    """
    deadline = time.monotonic() + stream_wait_seconds
    req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    try:
        resp = urllib.request.urlopen(req, timeout=stream_wait_seconds + 1.0)
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        _fail(f"could not open SSE stream: {exc}")
        return False

    try:
        while time.monotonic() < deadline:
            line = resp.readline()
            if not line:
                continue
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if text.startswith(":") or not text:
                continue
            if text.startswith("data:"):
                print(f"  observed SSE data line: {text[:80]}{'...' if len(text) > 80 else ''}")
                return True
    finally:
        try:
            resp.close()
        except Exception:                            # noqa: BLE001
            pass

    _fail(f"no SSE data line observed within {stream_wait_seconds}s")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Live Engines walkthrough (#121)")
    parser.add_argument("--anomaly-url", default="http://localhost:8082")
    parser.add_argument("--forecasting-url", default="http://localhost:8083")
    parser.add_argument("--rl-url", default="http://localhost:8084")
    parser.add_argument("--operator-ui-url", default="http://localhost:8090")
    parser.add_argument("--stream-wait-seconds", type=float, default=20.0,
                        help="how long to wait for at least one SSE event")
    args = parser.parse_args()

    print("step 1 — per-engine state endpoints")
    s1_ok = all([
        _check_engine_state("anomaly-detector", f"{args.anomaly_url}/api/v1/engine/state"),
        _check_engine_state("forecasting", f"{args.forecasting_url}/api/v1/engine/state"),
        _check_engine_state("rl-engine", f"{args.rl_url}/api/v1/engine/state"),
    ])
    if not s1_ok:
        return _fail("at least one /api/v1/engine/state probe failed")

    print("\nstep 2 — BFF snapshot fan-out")
    try:
        snapshot = _get_json(f"{args.operator_ui_url}/api/ui/engines/snapshot", timeout=10.0)
    except Exception as exc:                         # noqa: BLE001
        return _fail(f"/api/ui/engines/snapshot fetch failed: {exc}")
    if not _check_snapshot(snapshot):
        return 1

    print(f"\nstep 3 — SSE stream (waiting up to {args.stream_wait_seconds:.0f}s)")
    if not _read_one_sse_event(
        f"{args.operator_ui_url}/api/ui/engines/stream",
        args.stream_wait_seconds,
    ):
        return 1

    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
