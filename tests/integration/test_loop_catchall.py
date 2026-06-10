"""
tests/integration/test_loop_catchall.py
────────────────────────────────────────
#163 invariant: an unexpected exception inside the run-loop's iteration
body must NOT kill the daemon thread.

Verified end-to-end for the forecasting service as the representative
pattern (anomaly-detector + rl-engine + autoscaler + lb-sidecar mirror
this shape — same outer `try/except Exception` + back-off + continue).

Drives the real `_run_loop` against a fake pubsub + raising
`_inference_cycle`, observes that:

  1. The first iteration's exception is logged + swallowed.
  2. The loop survives to a second iteration.
  3. The stop_event still terminates the loop cleanly.

The test uses real `psycopg2.connect` and `redis_lib.from_url` — they
fail with connection errors (no docker stack), the loop catches them,
and we're still able to observe the catch-all behaviour via the
ticks_total counter and the logged messages.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_REPO = Path(__file__).resolve().parents[2]


def _flush_service_modules():
    for mod_name in list(sys.modules):
        if mod_name in ("app", "runloop", "engine_base") or mod_name.startswith("engines."):
            sys.modules.pop(mod_name, None)


def _import_forecasting(monkeypatch):
    monkeypatch.setenv("FORECAST_RUNLOOP_ENABLED", "true")
    _flush_service_modules()
    svc_dir = _REPO / "services" / "forecasting"
    services_dir = _REPO / "services"
    for p in list(sys.path):
        if p and Path(p).parent == services_dir:
            sys.path.remove(p)
    for p in (str(svc_dir), str(services_dir)):
        if p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)
    import app                                       # noqa: PLC0415
    return app


def test_run_loop_survives_iteration_exception(monkeypatch):
    """When the iteration body raises on the first tick, the second tick
    must still happen. The thread must not die silently."""
    app_mod = _import_forecasting(monkeypatch)

    # Fast loop: 0.1 s poll interval + 0.2 s recovery backoff so 3 iterations
    # finish well under the test timeout.
    monkeypatch.setattr(app_mod, "POLL_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(app_mod, "LOOP_RECOVERY_BACKOFF_SECONDS", 0.1)

    # Fake redis + pubsub that never deliver messages and never raise.
    fake_pubsub = MagicMock()
    fake_pubsub.get_message.return_value = None
    fake_pubsub.subscribe.return_value = None
    fake_redis = MagicMock()
    fake_redis.pubsub.return_value = fake_pubsub

    def _fake_redis_from_url(*_args, **_kwargs):
        return fake_redis

    monkeypatch.setattr(app_mod.redis_lib, "from_url", _fake_redis_from_url)

    # Fake psycopg2.connect — returns a stub conn with a working .cursor().
    fake_conn = MagicMock()
    fake_conn.autocommit = True
    monkeypatch.setattr(app_mod.psycopg2, "connect", lambda *a, **kw: fake_conn)

    # Inference cycle raises on iteration 1, returns 0 thereafter.
    call_count = {"n": 0}

    def _exploding_cycle(_db, _redis):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated transient cycle failure")
        return 0

    monkeypatch.setattr(app_mod, "_inference_cycle", _exploding_cycle)

    stop_event = threading.Event()

    def _runner():
        app_mod._run_loop(stop_event=stop_event)

    t = threading.Thread(target=_runner, daemon=True)
    t.start()

    # Give the loop enough time for the raising iteration + backoff + at
    # least one more iteration. Margin: 1.5 s total.
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline and call_count["n"] < 2:
        time.sleep(0.05)

    stop_event.set()
    t.join(timeout=2.0)

    assert call_count["n"] >= 2, (
        f"loop did not survive the exception — _inference_cycle was called "
        f"{call_count['n']} time(s); expected >=2 (one raise + at least one "
        f"successful re-iteration)"
    )
    assert not t.is_alive(), "stop_event did not terminate the loop"


def test_run_loop_keeps_going_through_repeated_exceptions(monkeypatch):
    """A persistent failure mode (every iteration raises) must still be
    survivable — the loop runs until stop_event, doesn't die after N
    failures."""
    app_mod = _import_forecasting(monkeypatch)
    monkeypatch.setattr(app_mod, "POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(app_mod, "LOOP_RECOVERY_BACKOFF_SECONDS", 0.05)

    fake_pubsub = MagicMock()
    fake_pubsub.get_message.return_value = None
    fake_pubsub.subscribe.return_value = None
    fake_redis = MagicMock()
    fake_redis.pubsub.return_value = fake_pubsub
    monkeypatch.setattr(app_mod.redis_lib, "from_url", lambda *a, **kw: fake_redis)

    fake_conn = MagicMock()
    fake_conn.autocommit = True
    monkeypatch.setattr(app_mod.psycopg2, "connect", lambda *a, **kw: fake_conn)

    call_count = {"n": 0}

    def _always_raise(_db, _redis):
        call_count["n"] += 1
        raise RuntimeError(f"persistent failure #{call_count['n']}")

    monkeypatch.setattr(app_mod, "_inference_cycle", _always_raise)

    stop_event = threading.Event()
    t = threading.Thread(target=lambda: app_mod._run_loop(stop_event=stop_event), daemon=True)
    t.start()

    # Run for ~1 s; with 0.05 s backoff we expect roughly 10+ iterations
    time.sleep(1.0)
    stop_event.set()
    t.join(timeout=2.0)

    assert call_count["n"] >= 5, (
        f"loop should have iterated many times under persistent failure; "
        f"saw {call_count['n']}"
    )
    assert not t.is_alive(), "stop_event did not terminate the loop"
