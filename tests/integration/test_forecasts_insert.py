"""
tests/integration/test_forecasts_insert.py
───────────────────────────────────────────
Integration test for #159: the forecasting service writes one row to the
`forecasts` hypertable per inference cycle, before publishing the envelope.

Runs in the unit-tests CI job — no Docker, no live DB. Mocks db_conn's
cursor to capture executed SQL + bound parameters, and asserts row count
grows across cycles.

Closes SOT §35.8 (predicted-RPS sparse) via FORECASTS_INSERT, the canonical
write constant in services/shared/queries.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO = Path(__file__).resolve().parents[2]
_FORECASTING = _REPO / "services" / "forecasting"
_SERVICES = _REPO / "services"

for p in (str(_FORECASTING), str(_SERVICES)):
    if p not in sys.path:
        sys.path.insert(0, p)

import app as forecasting_app                       # noqa: E402
from engine_base import Forecast                    # noqa: E402
from runloop import EnginePolicy                    # noqa: E402
from shared.queries import FORECASTS_INSERT         # noqa: E402


class _FakeCursor:
    """Captures every execute() call's SQL + params. Supports the
    context-manager protocol the cycle uses (`with db_conn.cursor() as cur`)."""
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return []   # empty history window — engine still produces a forecast


class _FakeConn:
    def __init__(self, cursor_cls=_FakeCursor):
        self._cursor = cursor_cls()

    def cursor(self):
        return self._cursor


class _FakeEngine:
    """Stub engine returning a fixed forecast — lets us drive _inference_cycle
    without loading the real moving_average engine and its window-size guards."""
    def __init__(self):
        self.calls = 0

    def forecast(self, history):
        self.calls += 1
        return Forecast(
            horizon_minutes=5,
            predicted_rps=42.5 + self.calls,   # distinct rps per cycle
            confidence_lower=30.0,
            confidence_upper=55.0,
        )


def _count_inserts(cursor: _FakeCursor) -> int:
    return sum(1 for sql, _ in cursor.calls if sql == FORECASTS_INSERT)


@pytest.fixture
def primed_app(monkeypatch):
    """Prime forecasting module globals so _inference_cycle has a valid engine
    and policy. Also stub out publish_envelope so the test doesn't depend on
    Redis. Restores originals afterwards."""
    original = {
        "_engine": forecasting_app._engine,
        "_policy": forecasting_app._policy,
        "_engine_name": forecasting_app._engine_name,
    }

    fake_engine = _FakeEngine()
    forecasting_app._engine = fake_engine
    forecasting_app._engine_name = "moving_average"
    forecasting_app._policy = EnginePolicy()

    publish_calls: list[dict] = []

    def _capture_publish(redis_client, *, channel, source, payload):
        publish_calls.append({"channel": channel, "source": source, "payload": payload})

    monkeypatch.setattr(forecasting_app, "publish_envelope", _capture_publish)

    yield fake_engine, publish_calls

    for k, v in original.items():
        setattr(forecasting_app, k, v)


def test_inference_cycle_writes_forecast_row(primed_app):
    _, publish_calls = primed_app
    db_conn = _FakeConn()
    redis_client = MagicMock()

    forecasting_app._inference_cycle(db_conn, redis_client)

    inserts = [c for c in db_conn._cursor.calls if c[0] == FORECASTS_INSERT]
    assert len(inserts) == 1

    _, params = inserts[0]
    # (time, horizon_minutes, predicted_rps, conf_lower, conf_upper, model_name, model_version)
    assert params[1] == 5
    assert params[2] == 43.5            # 42.5 + first call
    assert params[3] == 30.0
    assert params[4] == 55.0
    assert params[5] == "moving_average"
    assert params[6] is None
    # Default policy → publish happens
    assert len(publish_calls) == 1


def test_row_count_grows_across_cycles(primed_app):
    db_conn = _FakeConn()
    redis_client = MagicMock()

    for _ in range(5):
        forecasting_app._inference_cycle(db_conn, redis_client)

    assert _count_inserts(db_conn._cursor) == 5


def test_insert_runs_even_under_safe_mode(primed_app):
    """safe_mode gates the publish, not the observational write — operators
    still need to see what the engine would have predicted."""
    _, publish_calls = primed_app
    forecasting_app._policy = EnginePolicy(safe_mode=True)
    db_conn = _FakeConn()
    redis_client = MagicMock()

    forecasting_app._inference_cycle(db_conn, redis_client)

    assert _count_inserts(db_conn._cursor) == 1
    assert publish_calls == []          # publish suppressed under safe_mode


def test_insert_failure_does_not_block_publish(primed_app, capsys):
    """Persistence is observational. A DB hiccup must not stop the publish."""
    _, publish_calls = primed_app

    class _FailingCursor(_FakeCursor):
        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            if sql == FORECASTS_INSERT:
                raise RuntimeError("simulated DB outage")

    db_conn = _FakeConn(cursor_cls=_FailingCursor)
    redis_client = MagicMock()

    forecasting_app._inference_cycle(db_conn, redis_client)

    out = capsys.readouterr().out
    assert "forecasts insert failed" in out
    # Despite DB failure, publish still fires — observability gap, not safety gate
    assert len(publish_calls) == 1
