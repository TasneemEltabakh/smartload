"""
services/autoscaler/app.py
───────────────────────────
Phase 0 stub — wired to Redis and TimescaleDB, reports connectivity on /health.
Phase 1 (T1.3): subscribe to smartload.forecast, scale test-backend containers
                 via Docker SDK based on predicted_rps vs capacity.
"""

import os

import psycopg2
import redis as redis_lib
from flask import Flask, jsonify

app = Flask(__name__)

SERVICE_NAME = os.environ.get("SERVICE_NAME", "autoscaler")
PORT = int(os.environ.get("PORT", "8085"))
TIMESCALEDB_URL = os.environ.get(
    "TIMESCALEDB_URL",
    "postgresql://postgres:changeme@timescaledb:5432/smartloaddb",
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")
MIN_BACKENDS = int(os.environ.get("MIN_BACKENDS", "1"))
MAX_BACKENDS = int(os.environ.get("MAX_BACKENDS", "5"))


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
    code = 200 if status == "ok" else 503  # SOT §11: 503 on degraded, never 207
    return jsonify(
        {
            "status": status,
            "service": SERVICE_NAME,
            "redis": redis_ok,
            "timescaledb": db_ok,
            "config": {"min_backends": MIN_BACKENDS, "max_backends": MAX_BACKENDS},
            **({"errors": errors} if errors else {}),
        }
    ), code


@app.route("/")
def index():
    return jsonify({"service": SERVICE_NAME, "status": "running"})


if __name__ == "__main__":
    print(f"[{SERVICE_NAME}] starting on port {PORT}")
    app.run(host="0.0.0.0", port=PORT)
