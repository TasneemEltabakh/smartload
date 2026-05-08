"""
services/telemetry/app.py
─────────────────────────
Phase 1A (T1.1): receive OTLP/HTTP metrics from the OTel Collector and
persist them to the canonical `metrics` hypertable.

Per SOT §8.3 (Telemetry Service design contract):
  - We are the only writer of `metrics`. SQL goes through METRICS_INSERT
    in services/shared/queries.py — no inline DDL/DML here.
  - Receive → validate → batch insert → ACK. Synchronous writes are
    acceptable in the prototype (SOT §13 sync/async table).
  - On DB unavailability, telemetry must not be lost silently — log +
    counter on dropped writes; never let backpressure reach the data
    plane. The OTel SDK in NGINX is fire-and-forget by design, so we
    always 200 the collector regardless of DB state.
  - /health verifies Redis ping + TimescaleDB SELECT 1, returning
    200 ok or 503 degraded per SOT §11.

REST surface:
  POST /v1/metrics                         OTLP/HTTP-JSON ingress
  GET  /api/v1/metrics?service=&window=    SOT §11 read API
  GET  /api/v1/stats                       observability counters
  GET  /health                             liveness (200 / 503)
"""

from __future__ import annotations

import os
import re
import sys
import threading
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import redis as redis_lib
from flask import Flask, jsonify, request

# Resolve the canonical `shared/` module across two layouts:
#   container: /app/shared       (sibling of app.py — Dockerfile copies it)
#   dev / CI:  services/shared   (parent dir of services/telemetry/app.py)
# SOT §11: SQL constants in services/shared/queries.py are the single source
# of truth — never inline.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (_HERE, os.path.dirname(_HERE)):
    if os.path.isdir(os.path.join(_cand, "shared")):
        sys.path.insert(0, _cand)
        break
from shared.queries import METRICS_INSERT  # noqa: E402

app = Flask(__name__)

SERVICE_NAME    = os.environ.get("SERVICE_NAME", "telemetry")
PORT            = int(os.environ.get("PORT", "8081"))
TIMESCALEDB_URL = os.environ.get(
    "TIMESCALEDB_URL",
    "postgresql://postgres:changeme@timescaledb:5432/smartloaddb",
)
REDIS_URL          = os.environ.get("REDIS_URL", "redis://redis:6379")
READ_API_ROW_LIMIT = int(os.environ.get("READ_API_ROW_LIMIT", "10000"))


# ── observability counters (SOT §8.3: rows written / dropped / mean batch) ───

_stats_lock         = threading.Lock()
_rows_written       = 0
_batches_written    = 0
_rows_dropped_db    = 0
_rows_dropped_shape = 0


def _bump(field: str, n: int = 1) -> None:
    global _rows_written, _batches_written, _rows_dropped_db, _rows_dropped_shape
    with _stats_lock:
        if field == "rows_written":
            _rows_written += n
        elif field == "batches_written":
            _batches_written += n
        elif field == "rows_dropped_db":
            _rows_dropped_db += n
        elif field == "rows_dropped_shape":
            _rows_dropped_shape += n


# ── dependency probes (used by /health) ──────────────────────────────────────

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
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        conn.close()
        return True, None
    except Exception as exc:
        return False, str(exc)


# ── OTLP/HTTP-JSON parsing ───────────────────────────────────────────────────
# Reference: opentelemetry-proto, encoding=json subset.
#
# Envelope shape (excerpt):
#   { "resourceMetrics": [
#       { "resource": { "attributes": [...] },
#         "scopeMetrics": [
#           { "metrics": [
#               { "name": ..., "gauge"|"sum": { "dataPoints": [...] } }
#   ] } ] } ] }
#
# Histogram / summary / exponentialHistogram are not in the SmartLoad metric
# set (long-format gauges + counters per SOT §8.3); we count and drop them.

def _attr(attrs, key, default=None):
    for a in attrs or []:
        if a.get("key") == key:
            v = a.get("value", {})
            return (
                v.get("stringValue")
                or v.get("intValue")
                or v.get("doubleValue")
                or default
            )
    return default


def _datapoint_value(dp: dict):
    if "asDouble" in dp:
        try:
            return float(dp["asDouble"])
        except (TypeError, ValueError):
            return None
    if "asInt" in dp:
        try:
            return float(dp["asInt"])
        except (TypeError, ValueError):
            return None
    return None


def _datapoint_time(dp: dict):
    raw = dp.get("timeUnixNano") or dp.get("startTimeUnixNano")
    if raw is None:
        return None
    try:
        ns = int(raw)
        return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_otlp_to_rows(envelope: dict) -> list[tuple]:
    """Walk an OTLP/JSON envelope into `metrics` rows.

    Returns a list of (time, service, instance, metric_name, value) tuples
    in METRICS_INSERT order. Missing service.name → "unknown"; instance
    falls through service.instance.id → host.name → datapoint attr
    `instance` → "unknown".
    """
    rows: list[tuple] = []
    for rm in envelope.get("resourceMetrics", []) or []:
        res_attrs = rm.get("resource", {}).get("attributes", [])
        service   = _attr(res_attrs, "service.name", "unknown")
        instance  = _attr(res_attrs, "service.instance.id") or _attr(res_attrs, "host.name")
        for sm in rm.get("scopeMetrics", []) or []:
            for m in sm.get("metrics", []) or []:
                name = m.get("name")
                if not name:
                    continue
                series = m.get("gauge") or m.get("sum")
                if not series:
                    _bump("rows_dropped_shape")
                    continue
                for dp in series.get("dataPoints", []) or []:
                    val = _datapoint_value(dp)
                    ts  = _datapoint_time(dp)
                    if val is None or ts is None:
                        _bump("rows_dropped_shape")
                        continue
                    inst = instance or _attr(dp.get("attributes"), "instance", "unknown")
                    rows.append((ts, service, inst, name, val))
    return rows


# ── routes ───────────────────────────────────────────────────────────────────

@app.route("/v1/metrics", methods=["POST"])
def ingest_otlp():
    """Persist an OTLP/HTTP-JSON metrics export to TimescaleDB.

    Always returns 200 to the OTel Collector — backpressure must not reach
    the data plane (SOT §8.3). DB write failures are counted in
    /api/v1/stats.rows_dropped_db so the issue stays visible.
    """
    if request.content_type and "json" not in request.content_type.lower():
        return jsonify({"error": "expected application/json"}), 415

    envelope = request.get_json(silent=True)
    if not isinstance(envelope, dict):
        return jsonify({"error": "malformed OTLP body"}), 400

    rows = parse_otlp_to_rows(envelope)
    if not rows:
        return jsonify({"accepted": 0}), 200

    try:
        conn = psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5)
        try:
            with conn, conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, METRICS_INSERT, rows, page_size=500)
        finally:
            conn.close()
    except Exception as exc:
        _bump("rows_dropped_db", n=len(rows))
        app.logger.error("[%s] DB write failed: %s", SERVICE_NAME, exc)
        return jsonify({"accepted": 0, "dropped_db": len(rows)}), 200

    _bump("rows_written", n=len(rows))
    _bump("batches_written")
    return jsonify({"accepted": len(rows)}), 200


_WINDOW_RE    = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_WINDOW_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def _window_to_interval(text: str):
    """Parse a Prometheus-style window string into a Postgres interval text.

    Accepts e.g. "30s", "5m", "1h", "2d". Returns None for empty / malformed
    input so the caller can return 400.
    """
    if not text:
        return None
    m = _WINDOW_RE.match(text)
    if not m:
        return None
    n, unit = m.group(1), m.group(2)
    if int(n) <= 0:
        return None
    return f"{n} {_WINDOW_UNITS[unit]}"


@app.route("/api/v1/metrics", methods=["GET"])
def read_api():
    """SOT §11: GET /api/v1/metrics?service=&window= returns recent rows."""
    service = request.args.get("service", type=str)
    window  = request.args.get("window",  type=str)
    interval = _window_to_interval(window or "")
    if not service or not interval:
        return jsonify({
            "error":   "ValidationError",
            "message": "service and window are required; window must look like 30s/5m/1h/2d",
        }), 400

    try:
        conn = psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                # Fully parameterised — interval, service, and limit are bind
                # params per SOT §11 "no .format on SQL" rule.
                cur.execute(
                    """
                    SELECT time, service, instance, metric_name, value
                    FROM metrics
                    WHERE time > NOW() - %s::interval
                      AND service = %s
                    ORDER BY time DESC
                    LIMIT %s
                    """,
                    (interval, service, READ_API_ROW_LIMIT),
                )
                rows = [
                    {
                        "time":        r[0].isoformat() if r[0] else None,
                        "service":     r[1],
                        "instance":    r[2],
                        "metric_name": r[3],
                        "value":       r[4],
                    }
                    for r in cur.fetchall()
                ]
        finally:
            conn.close()
    except Exception as exc:
        return jsonify({"error": "DBError", "message": str(exc)}), 503

    return jsonify({"service": service, "window": window, "rows": rows}), 200


@app.route("/api/v1/stats", methods=["GET"])
def stats():
    with _stats_lock:
        return jsonify({
            "service":            SERVICE_NAME,
            "rows_written":       _rows_written,
            "batches_written":    _batches_written,
            "rows_dropped_db":    _rows_dropped_db,
            "rows_dropped_shape": _rows_dropped_shape,
        }), 200


@app.route("/health")
def health():
    redis_ok, redis_err = check_redis()
    db_ok, db_err = check_timescaledb()
    errors = [e for e in [redis_err, db_err] if e]
    status = "ok" if (redis_ok and db_ok) else "degraded"
    code = 200 if status == "ok" else 503  # SOT §11
    return jsonify({
        "status":      status,
        "service":     SERVICE_NAME,
        "redis":       redis_ok,
        "timescaledb": db_ok,
        **({"errors": errors} if errors else {}),
    }), code


@app.route("/")
def index():
    return jsonify({"service": SERVICE_NAME, "status": "running"})


if __name__ == "__main__":
    print(f"[{SERVICE_NAME}] starting on port {PORT}")
    app.run(host="0.0.0.0", port=PORT)
