"""
scripts/scenarios/_common.py
─────────────────────────────
Shared plumbing for the operator-runnable demo scenarios in this directory.

These are NOT pytest tests (that's tests/e2e/). They are dev-time, demo-time
scripts that narrate to the console: snapshot baseline, trigger a feature,
watch the expected response, print human-readable progress, exit 0 on success
and nonzero on failure.

This module concentrates the bits every script repeats:

  - Repo-path bootstrap so `services.shared.contracts` and the Python SDK
    (clients/python) import cleanly no matter the working directory.
  - Connection defaults that match tests/integration/conftest.py and the SDK
    constructor (REDIS_URL, the per-service SMARTLOAD_*_URL vars).
  - A small set of console-narration helpers (banner / step / ok / warn / fail)
    so every scenario reads the same way.
  - A Redis pub/sub poll loop built on services.shared.contracts.parse_envelope
    so scripts decode the canonical Envelope exactly the way every subscriber
    in the system does (channel TTL drop included).

Scripts import from here rather than restating the boilerplate; the actual
feature narration lives in each scenario file.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

# ── repo-path bootstrap ───────────────────────────────────────────────────────
# Make `services.shared.*` and the Python SDK importable whether the script is
# run from the repo root, from scripts/scenarios/, or anywhere else. Mirrors the
# pattern in scripts/bootstrap-config.py and tests/e2e/*/conftest.py.

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SDK_ROOT = _REPO_ROOT / "clients" / "python"
for _p in (str(_REPO_ROOT), str(_SDK_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── connection defaults (match tests/integration/conftest.py + SDK) ───────────
# Each scenario reads connection info from env vars with the same defaults as
# the integration suite, so a single set of overrides drives tests AND demos.

def redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379")


def policy_url() -> str:
    return os.environ.get("POLICY_URL", "http://localhost:8086")


def autoscaler_url() -> str:
    return os.environ.get("SMARTLOAD_AUTOSCALER_URL", "http://localhost:8085")


def anomaly_detector_url() -> str:
    return os.environ.get("SMARTLOAD_ANOMALY_DETECTOR_URL", "http://localhost:8082")


def forecasting_url() -> str:
    return os.environ.get("SMARTLOAD_FORECASTING_URL", "http://localhost:8083")


def operator_ui_url() -> str:
    return os.environ.get("SMARTLOAD_OPERATOR_UI_URL", "http://localhost:8090")


# ── console narration ─────────────────────────────────────────────────────────
# Plain ASCII markers (no Unicode) so the output renders the same on a Windows
# console, in CI logs, and when copy-pasted into the thesis reproducibility
# appendix.

def banner(title: str, summary: str) -> None:
    """One-line summary at the top describing what the script proves."""
    print(f"== {title} ==")
    print(summary)
    print()


def conn(label: str, value: str) -> None:
    print(f"  {label:18s} {value}")


def step(n: int, title: str) -> None:
    print(f"\nStep {n}: {title}")


def ok(msg: str) -> None:
    print(f"  OK   {msg}")


def warn(msg: str) -> None:
    print(f"  WARN {msg}")


def info(msg: str) -> None:
    print(f"  ..   {msg}")


def fail(msg: str) -> int:
    """Print a failure line to stderr and return the process exit code 1."""
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


def done(msg: str = "scenario complete") -> int:
    print(f"\nPASS - {msg}")
    return 0


# ── Redis helpers ─────────────────────────────────────────────────────────────

def open_redis(url: str):
    """Return a decode-less redis client, or None (with a warning) if redis-py
    is not installed. Scripts that can still demonstrate something over HTTP
    degrade gracefully instead of crashing on the import."""
    try:
        import redis as redis_lib  # local import: keeps import cost off the help path
    except ImportError:
        warn("redis-py not installed -- channel checks will be skipped")
        return None
    return redis_lib.from_url(url, decode_responses=False)


def subscribe(redis_client, channel: str):
    """SUBSCRIBE to `channel` and drain the subscribe confirmation. Returns the
    pubsub handle (caller is responsible for .close())."""
    pubsub = redis_client.pubsub()
    pubsub.subscribe(channel)
    pubsub.get_message(timeout=1.0)  # consume the subscribe confirmation
    return pubsub


def wait_for_envelope(
    pubsub,
    channel: str,
    predicate: Callable[[dict, dict], bool],
    timeout: float,
) -> tuple[dict, dict] | None:
    """Poll `pubsub` for up to `timeout` seconds. Decode each message with the
    canonical envelope parser (so channel-TTL drops behave exactly like every
    other subscriber), and return the first (payload, envelope_meta) for which
    `predicate(payload, meta)` is True. Returns None on timeout.
    """
    from services.shared.contracts import parse_envelope

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if msg is None or msg.get("type") != "message":
            continue
        parsed = parse_envelope(msg.get("data", b""), channel=channel)
        if parsed is None:
            continue
        payload, meta = parsed
        try:
            if predicate(payload, meta):
                return payload, meta
        except Exception:  # noqa: BLE001 — a bad predicate must not crash the poll
            continue
    return None


def http_get_json(url: str, timeout: float = 10.0) -> tuple[int, Any]:
    """GET `url`, return (status_code, parsed_json_or_text). Never raises on a
    non-2xx; the caller decides what a given status means for the scenario."""
    import httpx

    r = httpx.get(url, timeout=timeout)
    try:
        body = r.json()
    except (ValueError, TypeError):
        body = r.text
    return r.status_code, body
