"""
tests/unit/anomaly-detector/test_anomaly_history.py
─────────────────────────────────────────────────────
Unit tests for the anomaly-detector read endpoint GET /api/v1/anomaly/history.
Exercises the Flask route through a test client with psycopg2 fully mocked —
no live DB.

Coverage:
  1. JSON response shape — history rows, distinct backends list, window_seconds.
  2. Parameter validation + caps — window/limit defaults, negatives, over-cap.
  3. Optional backend filter — bound into the NULL-guarded query params.
  4. The canonical query constant (ANOMALY_HISTORY_QUERY) is the SQL executed.
  5. Graceful degrade — a DB failure returns an empty result with HTTP 200.
"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Load the anomaly-detector service modules with the service dir at the FRONT of
# sys.path. Sibling services ship same-named top-level modules (app / manual /
# runloop / engine_base), so under pytest's prepend import mode the wrong one
# can be cached by another service's test.
#
# Re-importing `app` re-runs ServiceMetrics(...), which registers into the
# process-global Prometheus registry and would raise "Duplicated timeseries" on
# a second import in the same session. So only purge + re-import when the cached
# `app` is NOT already this service's; otherwise reuse it as-is.
_REPO = Path(__file__).resolve().parents[2].parent
_SERVICE = _REPO / "services" / "anomaly-detector"
_SERVICES = _REPO / "services"

def _drop_prometheus_collectors(prefix: str) -> None:
    """Unregister any default-registry collectors for `prefix` so a fresh import
    of this service's `app` (which builds ServiceMetrics at module scope) does
    not raise "Duplicated timeseries". Importing `app` more than once per
    process — across this test and a sibling test that also re-imports it —
    would otherwise collide on the process-global Prometheus registry."""
    from prometheus_client import REGISTRY

    for collector in list(getattr(REGISTRY, "_collector_to_names", {})):
        names = REGISTRY._collector_to_names.get(collector, set())
        if any(n.startswith(prefix) for n in names):
            try:
                REGISTRY.unregister(collector)
            except KeyError:
                pass


_cached = sys.modules.get("app")
_is_ours = bool(
    _cached
    and getattr(_cached, "SERVICE_NAME", None) == "anomaly-detector"
)
if _is_ours:
    anomaly_app = _cached
else:
    for _name in ("app", "manual", "decisions", "cluster_client", "runloop", "engine_base"):
        sys.modules.pop(_name, None)
    for _p in (str(_SERVICES), str(_SERVICE)):
        if _p in sys.path:
            sys.path.remove(_p)
        sys.path.insert(0, _p)   # service dir ends up first, services/ second
    _drop_prometheus_collectors("anomaly_detector")
    anomaly_app = importlib.import_module("app")
    # Leave the default registry clean so a sibling test that purges sys.modules
    # and re-imports this service's `app` (e.g. test_simulate_isolate) can
    # rebuild ServiceMetrics without a duplicate-registration collision.
    _drop_prometheus_collectors("anomaly_detector")
from shared.queries import ANOMALY_HISTORY_QUERY    # noqa: E402


_T0 = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)
_T1 = datetime(2026, 6, 14, 11, 59, 0, tzinfo=timezone.utc)

# (time, backend_id, status, score)
_ROWS = [
    (_T0, "backend_1", "unhealthy", 1.0),
    (_T1, "backend_2", "healthy", 0.0),
]


class _FakeCursor:
    """Captures execute() calls and returns a canned row set."""
    def __init__(self, rows):
        self._rows = rows
        self.calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self.cursor_obj = _FakeCursor(rows)

    def cursor(self):
        return self.cursor_obj

    def close(self):
        pass


def _client_with_rows(monkeypatch, rows):
    holder: dict = {}

    def _connect(*a, **k):
        conn = _FakeConn(rows)
        holder["cursor"] = conn.cursor_obj
        return conn

    monkeypatch.setattr(anomaly_app.psycopg2, "connect", _connect)
    anomaly_app.app.config.update(TESTING=True)
    return anomaly_app.app.test_client(), holder


def test_response_shape(monkeypatch):
    client, _ = _client_with_rows(monkeypatch, _ROWS)
    r = client.get("/api/v1/anomaly/history")
    assert r.status_code == 200
    body = r.get_json()

    assert set(body) == {"history", "backends", "window_seconds"}
    assert body["window_seconds"] == 3600
    assert len(body["history"]) == 2

    first = body["history"][0]
    assert first["time"] == _T0.isoformat()
    assert first["backend_id"] == "backend_1"
    assert first["status"] == "unhealthy"
    assert first["score"] == 1.0

    # Distinct backends, first-appearance order.
    assert body["backends"] == ["backend_1", "backend_2"]


def test_uses_canonical_query_and_default_params(monkeypatch):
    client, holder = _client_with_rows(monkeypatch, _ROWS)
    client.get("/api/v1/anomaly/history")

    sql, params = holder["cursor"].calls[0]
    assert sql == ANOMALY_HISTORY_QUERY
    # (interval, backend, backend, limit) — no filter → backend is None twice.
    assert params == ("3600 seconds", None, None, 500)


def test_backend_filter_bound_twice(monkeypatch):
    client, holder = _client_with_rows(monkeypatch, _ROWS)
    client.get("/api/v1/anomaly/history?backend=backend_1")

    _, params = holder["cursor"].calls[0]
    assert params == ("3600 seconds", "backend_1", "backend_1", 500)


@pytest.mark.parametrize(
    "qs,exp_interval,exp_limit,exp_window",
    [
        ("",                       "3600 seconds",  500, 3600),
        ("?window=60&limit=10",    "60 seconds",     10,   60),
        ("?window=0",              "3600 seconds",  500, 3600),   # non-positive → default
        ("?window=-5",             "3600 seconds",  500, 3600),
        ("?window=999999",         "86400 seconds", 500, 86400),  # capped
        ("?limit=0",               "3600 seconds",  500, 3600),   # non-positive → default
        ("?limit=999999",          "3600 seconds", 5000, 3600),   # capped
        ("?window=abc&limit=xyz",  "3600 seconds",  500, 3600),   # non-int → default
    ],
)
def test_param_validation_and_caps(monkeypatch, qs, exp_interval, exp_limit, exp_window):
    client, holder = _client_with_rows(monkeypatch, _ROWS)
    r = client.get("/api/v1/anomaly/history" + qs)
    assert r.status_code == 200
    assert r.get_json()["window_seconds"] == exp_window

    _, params = holder["cursor"].calls[0]
    assert params == (exp_interval, None, None, exp_limit)


def test_db_failure_returns_empty_200(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(anomaly_app.psycopg2, "connect", _boom)
    anomaly_app.app.config.update(TESTING=True)
    client = anomaly_app.app.test_client()

    r = client.get("/api/v1/anomaly/history?window=120")
    assert r.status_code == 200
    assert r.get_json() == {"history": [], "backends": [], "window_seconds": 120}
