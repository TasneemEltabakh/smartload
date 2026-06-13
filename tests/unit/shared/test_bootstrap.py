"""
tests/unit/shared/test_bootstrap.py
─────────────────────────────────────
Unit tests for services/shared/bootstrap.py. Hermetic: the connectivity probes
take injected connectors and the signal wiring takes an injected registrar, so
no redis / psycopg2 / real signals are touched.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

_SERVICES = Path(__file__).resolve().parents[3] / "services"
if str(_SERVICES) not in sys.path:
    sys.path.insert(0, str(_SERVICES))

from shared.bootstrap import (  # noqa: E402
    add_shared_to_path,
    check_redis,
    check_timescaledb,
    find_shared_root,
    install_signal_handlers,
    liveness_stale_seconds,
)


# ── shared/ path resolution ─────────────────────────────────────────────────────

def test_find_shared_root_same_dir(tmp_path):
    (tmp_path / "shared").mkdir()
    fake_file = tmp_path / "app.py"
    assert find_shared_root(str(fake_file)) == str(tmp_path)


def test_find_shared_root_parent_dir(tmp_path):
    (tmp_path / "shared").mkdir()
    sub = tmp_path / "svc"
    sub.mkdir()
    fake_file = sub / "app.py"
    assert find_shared_root(str(fake_file)) == str(tmp_path)


def test_find_shared_root_missing(tmp_path):
    fake_file = tmp_path / "svc" / "app.py"
    assert find_shared_root(str(fake_file)) is None


def test_add_shared_to_path_inserts_and_is_idempotent(tmp_path):
    (tmp_path / "shared").mkdir()
    fake_file = tmp_path / "app.py"
    root = str(tmp_path)
    try:
        assert add_shared_to_path(str(fake_file)) == root
        assert root in sys.path
        # idempotent — second call doesn't duplicate the entry
        add_shared_to_path(str(fake_file))
        assert sys.path.count(root) == 1
    finally:
        while root in sys.path:
            sys.path.remove(root)


# ── connectivity probes ──────────────────────────────────────────────────────────

class _OkClient:
    def ping(self):
        return True

    def close(self):
        return None


def test_check_redis_ok():
    ok, detail = check_redis("redis://x", client_factory=lambda url, timeout: _OkClient())
    assert ok is True and detail is None


def test_check_redis_ping_fails():
    class _Bad:
        def ping(self):
            raise RuntimeError("no pong")

    ok, detail = check_redis("redis://x", client_factory=lambda url, timeout: _Bad())
    assert ok is False and "no pong" in detail


def test_check_redis_connect_fails():
    def _boom(url, timeout):
        raise ConnectionError("refused")

    ok, detail = check_redis("redis://x", client_factory=_boom)
    assert ok is False and "refused" in detail


def test_check_timescaledb_ok():
    ok, detail = check_timescaledb("postgresql://x", connect=lambda url, timeout: _OkClient())
    assert ok is True and detail is None


def test_check_timescaledb_connect_fails():
    def _boom(url, timeout):
        raise RuntimeError("auth failed")

    ok, detail = check_timescaledb("postgresql://x", connect=_boom)
    assert ok is False and "auth failed" in detail


# ── signal handling ──────────────────────────────────────────────────────────────

def test_install_signal_handlers_registers_all():
    captured = {}

    def _register(sig, handler):
        captured[sig] = handler

    def _on_shutdown(signum, frame):
        pass

    ok = install_signal_handlers(_on_shutdown, register=_register)
    assert ok is True
    assert captured[signal.SIGTERM] is _on_shutdown
    assert captured[signal.SIGINT] is _on_shutdown


def test_install_signal_handlers_off_main_thread_is_swallowed():
    def _register(sig, handler):
        raise ValueError("signal only works in main thread")

    assert install_signal_handlers(lambda *_: None, register=_register) is False


# ── liveness ──────────────────────────────────────────────────────────────────────

def test_liveness_stale_seconds_default_factor():
    assert liveness_stale_seconds(10) == 50.0


def test_liveness_stale_seconds_custom_factor():
    assert liveness_stale_seconds(10, factor=3) == 30.0
