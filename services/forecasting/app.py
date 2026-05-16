"""
services/forecasting/app.py
────────────────────────────
Phase 2 (N2.2): Prophet-based workload forecasting.

Every FORECAST_INTERVAL_SECONDS (default 60 s):
  1. Query last 1 h of request_count from TimescaleDB via FORECAST_QUERY.
  2. Run ProphetForecaster.predict() — live-fit → static → moving average.
  3. Publish ForecastResult to smartload.forecast via canonical Envelope.

Fallback chain inside ProphetForecaster:
  prophet_live (fresh fit on recent data)
    → prophet_static (Alibaba-pretrained model, if pkl exists)
    → moving_average (always succeeds)
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pandas as pd
import psycopg2
import redis as redis_lib
from flask import Flask, jsonify

# Resolve shared/ across container layout (/app/shared) and dev layout
# (services/shared relative to this file). Same pattern as telemetry/app.py.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (_HERE, os.path.dirname(_HERE)):
    if os.path.isdir(os.path.join(_cand, "shared")):
        sys.path.insert(0, _cand)
        break
from shared.contracts import ForecastResult, publish_envelope  # noqa: E402
from shared.queries import FORECAST_QUERY                       # noqa: E402

from forecaster import ProphetForecaster  # noqa: E402

app = Flask(__name__)

SERVICE_NAME              = os.environ.get("SERVICE_NAME", "forecasting")
PORT                      = int(os.environ.get("PORT", "8083"))
TIMESCALEDB_URL           = os.environ.get(
    "TIMESCALEDB_URL",
    "postgresql://postgres:changeme@timescaledb:5432/smartloaddb",
)
REDIS_URL                 = os.environ.get("REDIS_URL", "redis://redis:6379")
FORECAST_INTERVAL_SECONDS = int(os.environ.get("FORECAST_INTERVAL_SECONDS", "60"))
FORECAST_WINDOW           = os.environ.get("FORECAST_WINDOW", "1 hour")

# ── clients ───────────────────────────────────────────────────────────────────

_redis_client: redis_lib.Redis | None = None
_forecaster: ProphetForecaster | None = None


def _get_redis() -> redis_lib.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis_lib.from_url(REDIS_URL, socket_connect_timeout=3)
    return _redis_client


def _get_forecaster() -> ProphetForecaster:
    global _forecaster
    if _forecaster is None:
        _forecaster = ProphetForecaster()
        _forecaster.load_pretrained()
    return _forecaster


# ── TimescaleDB query ─────────────────────────────────────────────────────────

def _query_request_rate() -> pd.DataFrame:
    """
    Execute FORECAST_QUERY and return a Prophet-ready (ds, y) DataFrame.
    Returns empty DataFrame on failure.
    """
    try:
        conn = psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute(FORECAST_QUERY, (FORECAST_WINDOW,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as exc:
        print(f"[{SERVICE_NAME}] DB query failed: {exc}", flush=True)
        return pd.DataFrame(columns=["ds", "y"])

    if not rows:
        return pd.DataFrame(columns=["ds", "y"])

    df = pd.DataFrame(rows, columns=["ds", "y"])
    df["ds"] = pd.to_datetime(df["ds"]).dt.tz_localize(None)
    df["y"] = pd.to_numeric(df["y"], errors="coerce").fillna(0.0)
    return df.sort_values("ds").reset_index(drop=True)


# ── connectivity checks (for /health) ────────────────────────────────────────

def check_redis() -> tuple[bool, str | None]:
    try:
        _get_redis().ping()
        return True, None
    except Exception as exc:
        return False, str(exc)


def check_timescaledb() -> tuple[bool, str | None]:
    try:
        conn = psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5)
        conn.close()
        return True, None
    except Exception as exc:
        return False, str(exc)


# ── forecast loop ─────────────────────────────────────────────────────────────

def _forecast_loop() -> None:
    forecaster = _get_forecaster()
    r = _get_redis()

    print(f"[{SERVICE_NAME}] forecast loop started "
          f"(interval={FORECAST_INTERVAL_SECONDS}s)", flush=True)

    while True:
        try:
            df = _query_request_rate()
            result: ForecastResult = forecaster.predict(df)
            publish_envelope(r, "smartload.forecast", SERVICE_NAME, result)
            print(
                f"[{SERVICE_NAME}] published forecast: "
                f"rps={result.predicted_rps:.2f} "
                f"[{result.confidence_lower:.2f}, {result.confidence_upper:.2f}] "
                f"model={result.model_id}",
                flush=True,
            )
        except Exception as exc:
            print(f"[{SERVICE_NAME}] cycle error: {exc}", flush=True)

        time.sleep(FORECAST_INTERVAL_SECONDS)


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    redis_ok, redis_err = check_redis()
    db_ok, db_err = check_timescaledb()
    errors = [e for e in [redis_err, db_err] if e]
    status = "ok" if (redis_ok and db_ok) else "degraded"
    code = 200 if status == "ok" else 503  # SOT §11: 503 on degraded, never 207
    return jsonify(
        {
            "status": status,
            "service": SERVICE_NAME,
            "redis": redis_ok,
            "timescaledb": db_ok,
            **({"errors": errors} if errors else {}),
        }
    ), code


@app.route("/")
def index():
    return jsonify({"service": SERVICE_NAME, "status": "running"})


# ── startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _get_forecaster()

    t = threading.Thread(target=_forecast_loop, daemon=True)
    t.start()

    print(f"[{SERVICE_NAME}] starting on port {PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT)
