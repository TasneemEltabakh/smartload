"""
services/anomaly-detector/app.py
─────────────────────────────────
Phase 0 stub — wired to Redis and TimescaleDB, reports connectivity on /health.
Phase 1 (N1.1): threshold-based anomaly detection, publishes AnomalyEvent to
                 smartload.anomaly every POLL_INTERVAL_SECONDS.
Phase 2 (N2.1): replace threshold logic with Isolation Forest model.
"""

import os

import psycopg2
import redis as redis_lib
from flask import Flask, jsonify

app = Flask(__name__)

SERVICE_NAME = os.environ.get("SERVICE_NAME", "anomaly-detector")
PORT = int(os.environ.get("PORT", "8082"))
TIMESCALEDB_URL = os.environ.get(
    "TIMESCALEDB_URL",
    "postgresql://postgres:changeme@timescaledb:5432/smartloaddb",
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")


def check_redis():
    try:
        r = redis_lib.from_url(REDIS_URL, socket_connect_timeout=3)
        r.ping()
        return True, None
    except Exception as exc:
        return False, str(exc)


def check_timescaledb():
    try:
        conn = psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5)
        conn.close()
        return True, None
    except Exception as exc:
        return False, str(exc)


@app.route("/health")
def health():
    redis_ok, redis_err = check_redis()
    db_ok, db_err = check_timescaledb()
    errors = [e for e in [redis_err, db_err] if e]
    status = "ok" if (redis_ok and db_ok) else "degraded"
    code = 200 if status == "ok" else 207
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


if __name__ == "__main__":
    print(f"[{SERVICE_NAME}] starting on port {PORT}")
    app.run(host="0.0.0.0", port=PORT)
