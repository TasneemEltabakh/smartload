"""
services/shared/bootstrap.py
────────────────────────────
Service-startup plumbing shared across the control-plane services:

  - locating the ``shared/`` package on sys.path (the 6-line loop copied into
    every service's app.py);
  - Redis / TimescaleDB connectivity probes returning the ``(ok, detail)``
    contract every ``/health`` already uses;
  - SIGTERM / SIGINT wiring — only rl-engine + lb-sidecar had it, so
    anomaly-detector / forecasting / autoscaler leaked a pubsub subscription on
    ``compose down``;
  - the #163 liveness-staleness helper (``5 × poll_interval``).

Connectors and the signal registrar are **injectable**, and the heavy imports
(redis / psycopg2) are lazy, so this unit-tests without those libraries or real
signals.

Module-first: not yet wired into the services — adoption is a separate
CI-watched step since it touches startup (which has no unit coverage).
"""

from __future__ import annotations

import os
import signal
import sys
from typing import Optional


# ── shared/ path resolution ────────────────────────────────────────────────────

def find_shared_root(start_file: str) -> Optional[str]:
    """Return the directory containing a ``shared/`` package, searching the
    file's own dir then its parent — the container layout (``/app/shared``) and
    the dev layout (``services/shared/`` next to the service dir). None if
    neither has one."""
    here = os.path.dirname(os.path.abspath(start_file))
    for cand in (here, os.path.dirname(here)):
        if os.path.isdir(os.path.join(cand, "shared")):
            return cand
    return None


def add_shared_to_path(start_file: str) -> Optional[str]:
    """Locate ``shared/`` relative to ``start_file`` and prepend it to sys.path.
    Returns the path added (or None if not found). Idempotent."""
    root = find_shared_root(start_file)
    if root and root not in sys.path:
        sys.path.insert(0, root)
    return root


# ── connectivity probes — (ok, detail) ─────────────────────────────────────────

def check_redis(url, *, timeout=3, client_factory=None):
    """Ping Redis. Returns ``(True, None)`` on success, ``(False, reason)`` on
    failure. ``client_factory(url, timeout) -> client`` (with ``.ping()``) is
    injectable for tests; defaults to ``redis.from_url``."""
    try:
        if client_factory is None:
            import redis as _redis  # lazy
            client = _redis.from_url(url, socket_connect_timeout=timeout)
        else:
            client = client_factory(url, timeout)
        client.ping()
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def check_timescaledb(url, *, timeout=5, connect=None):
    """Open + close a TimescaleDB connection. Returns ``(True, None)`` /
    ``(False, reason)``. ``connect(url, timeout) -> conn`` (with ``.close()``)
    is injectable for tests; defaults to ``psycopg2.connect``."""
    try:
        if connect is None:
            import psycopg2  # lazy
            conn = psycopg2.connect(url, connect_timeout=timeout)
        else:
            conn = connect(url, timeout)
        conn.close()
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# ── signal handling ─────────────────────────────────────────────────────────────

def install_signal_handlers(on_shutdown, *, signals=(signal.SIGTERM, signal.SIGINT),
                            register=signal.signal) -> bool:
    """Wire ``signals`` to call ``on_shutdown(signum, frame)``.

    Returns True if installed, False if it couldn't be — ``signal.signal``
    raises ``ValueError`` when called off the main thread (test runners, worker
    threads), which is swallowed so those contexts aren't broken. ``register``
    is injectable for tests."""
    try:
        for sig in signals:
            register(sig, on_shutdown)
        return True
    except ValueError:
        return False


# ── liveness ────────────────────────────────────────────────────────────────────

def liveness_stale_seconds(poll_interval_seconds, factor=5) -> float:
    """The #163 staleness threshold: a loop that hasn't ticked in
    ``factor × poll_interval`` seconds is considered stale by ``/health``."""
    return float(factor) * float(poll_interval_seconds)
