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
from shared import config                                       # noqa: E402
from shared.logging_setup import install_correlation_middleware # noqa: E402

app = Flask(__name__)
# Per-request correlation IDs (#143).
install_correlation_middleware(app)

# Env via the shared typed config helpers (v1.0.7av). Behaviour-preserving.
SERVICE_NAME    = config.env_str("SERVICE_NAME", "telemetry")
PORT            = config.env_int("PORT", 8081)
TIMESCALEDB_URL = config.timescaledb_url()
REDIS_URL          = config.redis_url()
READ_API_ROW_LIMIT = config.env_int("READ_API_ROW_LIMIT", 10000)


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
    in METRICS_INSERT order. Missing service.name → "unknown". Instance
    resolution prefers the datapoint attribute (set per-request, e.g. the
    NGINX upstream backend), then resource service.instance.id, then
    host.name, then "unknown". Per-datapoint priority is what makes the
    `metrics.instance` column reflect the request's *subject* rather than
    the *emitter* (e.g. the load-balancer shipper sidecar).
    """
    rows: list[tuple] = []
    for rm in envelope.get("resourceMetrics", []) or []:
        res_attrs    = rm.get("resource", {}).get("attributes", [])
        service      = _attr(res_attrs, "service.name", "unknown")
        res_instance = _attr(res_attrs, "service.instance.id") or _attr(res_attrs, "host.name")
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
                    inst = _attr(dp.get("attributes"), "instance") or res_instance or "unknown"
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


@app.route("/api/v1/metrics/rpm", methods=["GET"])
def metrics_rpm():
    """Per-minute request-rate time series for the operator UI throughput card.

    ?window=N — number of 1-minute buckets to return (default 24, cap 240).

    Each bucket sums request_count rows in its minute and normalises to a
    per-second rate the same way FORECAST_QUERY does (SUM(value) / 60). The
    `total_requests` field is the raw sum over the window (no /60) so the
    UI can show "X requests in last Y minutes". On any DB failure we return
    a zeroed response with HTTP 200 — operator-UI must degrade gracefully
    rather than render an error state for a chart card.
    """
    try:
        window = int(request.args.get("window", 24))
    except (TypeError, ValueError):
        window = 24
    if window <= 0:
        window = 24
    window = min(window, 240)
    interval = f"{window} minutes"

    empty = {"buckets": [], "current_rpm": 0, "total_requests": 0}

    try:
        conn = psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                # Parameterised interval cast (SOT §11). Same per-second
                # normalisation as FORECAST_QUERY so the operator-UI's
                # throughput chart and the forecasting engine see the same
                # numbers from the same metric.
                cur.execute(
                    """
                    SELECT
                        time_bucket('1 minute', time) AS bucket,
                        SUM(value)::float / 60.0      AS request_rate,
                        SUM(value)::float             AS request_total
                    FROM metrics
                    WHERE time > NOW() - %s::interval
                      AND metric_name = 'request_count'
                    GROUP BY bucket
                    ORDER BY bucket ASC
                    """,
                    (interval,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:                                # noqa: BLE001
        app.logger.warning("[%s] rpm query failed: %s", SERVICE_NAME, exc)
        return jsonify(empty), 200

    if not rows:
        return jsonify(empty), 200

    all_buckets = [
        {
            "time": (bucket.isoformat() if bucket is not None else None),
            "rpm":  int(round((rate or 0.0) * 60.0)),
            "_total": int(round(t or 0.0)),
            "_bucket": bucket,
        }
        for (bucket, rate, t) in rows
    ]

    # The last bucket is partial whenever its start lies inside the current
    # minute (which is almost always true when we polled mid-minute). Splitting
    # it off prevents the sparkline from showing a false dip on its right edge
    # and lets `current_rpm` reflect the most-recent *complete* minute — what
    # an operator actually wants to read as "current throughput".
    now = datetime.now(timezone.utc)
    current_minute_start = now.replace(second=0, microsecond=0)
    last = all_buckets[-1]
    partial = None
    complete = all_buckets
    if last["_bucket"] is not None and last["_bucket"] >= current_minute_start:
        elapsed = max(1.0, (now - last["_bucket"]).total_seconds())
        partial = {
            "time":               last["time"],
            "rpm_so_far":         last["rpm"],
            "rows_so_far":        last["_total"],
            "elapsed_seconds":    round(elapsed, 1),
            # Linear projection of the partial bucket to a full-minute rate.
            # Useful as a "right now" indicator without contaminating the chart.
            "projected_rpm":      int(round(last["_total"] * 60.0 / elapsed)),
        }
        complete = all_buckets[:-1]

    buckets = [{"time": b["time"], "rpm": b["rpm"]} for b in complete]
    current_rpm    = buckets[-1]["rpm"] if buckets else 0
    total_requests = int(round(sum(b["_total"] for b in complete)))
    return jsonify({
        "buckets":        buckets,
        "current_rpm":    current_rpm,
        "total_requests": total_requests,
        "partial":        partial,
    }), 200


@app.route("/api/v1/metrics/latency", methods=["GET"])
def metrics_latency():
    """p50 / p95 / p99 latency over a rolling window in seconds.

    ?window=N — window length in seconds (default 60, cap 3600).

    Uses percentile_cont over the `request_latency_ms` series. If fewer than
    10 samples sit inside the window the percentiles are reported as null
    (with the real sample count) so the UI can show "insufficient data"
    rather than a misleading single-sample p95. DB failures collapse to a
    zero-sample response with HTTP 200 — same degrade-don't-error rule as
    the rpm endpoint."""
    try:
        window = int(request.args.get("window", 60))
    except (TypeError, ValueError):
        window = 60
    if window <= 0:
        window = 60
    window = min(window, 3600)
    interval = f"{window} seconds"

    empty = {"p50_ms": None, "p95_ms": None, "p99_ms": None, "samples": 0}

    try:
        conn = psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        percentile_cont(0.5)  WITHIN GROUP (ORDER BY value) AS p50,
                        percentile_cont(0.95) WITHIN GROUP (ORDER BY value) AS p95,
                        percentile_cont(0.99) WITHIN GROUP (ORDER BY value) AS p99,
                        COUNT(*)                                            AS samples
                    FROM metrics
                    WHERE time > NOW() - %s::interval
                      AND metric_name = 'request_latency_ms'
                    """,
                    (interval,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:                                # noqa: BLE001
        app.logger.warning("[%s] latency query failed: %s", SERVICE_NAME, exc)
        return jsonify(empty), 200

    if not row:
        return jsonify(empty), 200

    p50, p95, p99, samples = row
    samples = int(samples or 0)
    if samples < 10:
        return jsonify({
            "p50_ms":  None,
            "p95_ms":  None,
            "p99_ms":  None,
            "samples": samples,
        }), 200

    return jsonify({
        "p50_ms":  int(round(p50)) if p50 is not None else None,
        "p95_ms":  int(round(p95)) if p95 is not None else None,
        "p99_ms":  int(round(p99)) if p99 is not None else None,
        "samples": samples,
    }), 200


@app.route("/api/v1/metrics/slo", methods=["GET"])
def metrics_slo():
    """SLO compliance: percent of requests with latency <= slo_p95 over window.

    Query params:
      ?window=N    — window length in seconds (default 3600, cap 86400).
      ?slo_p95=N   — latency budget in ms (default 200).

    Computes both the compliant count and the violation count in one pass
    so the UI can show "99.2% compliant (37 violations / 4621 samples)"
    without a second round-trip. Empty / failed DB → zero-sample response
    with HTTP 200 so the SLO card degrades gracefully."""
    try:
        window = int(request.args.get("window", 3600))
    except (TypeError, ValueError):
        window = 3600
    if window <= 0:
        window = 3600
    window = min(window, 86400)

    try:
        slo_p95 = int(request.args.get("slo_p95", 200))
    except (TypeError, ValueError):
        slo_p95 = 200
    if slo_p95 <= 0:
        slo_p95 = 200

    interval = f"{window} seconds"

    empty = {
        "slo_p95_ms":     slo_p95,
        "window_seconds": window,
        "samples":        0,
        "compliant_pct":  0.0,
        "violations":     0,
    }

    try:
        conn = psycopg2.connect(TIMESCALEDB_URL, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                # COUNT(*) FILTER (...) keeps both counts in one scan. SLO
                # threshold is a bind parameter — never an f-string into SQL.
                cur.execute(
                    """
                    SELECT
                        COUNT(*)                                AS samples,
                        COUNT(*) FILTER (WHERE value <= %s)     AS compliant
                    FROM metrics
                    WHERE time > NOW() - %s::interval
                      AND metric_name = 'request_latency_ms'
                    """,
                    (slo_p95, interval),
                )
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:                                # noqa: BLE001
        app.logger.warning("[%s] slo query failed: %s", SERVICE_NAME, exc)
        return jsonify(empty), 200

    if not row:
        return jsonify(empty), 200

    samples   = int(row[0] or 0)
    compliant = int(row[1] or 0)
    if samples == 0:
        return jsonify(empty), 200
    violations    = samples - compliant
    compliant_pct = round(100.0 * compliant / samples, 2)
    return jsonify({
        "slo_p95_ms":     slo_p95,
        "window_seconds": window,
        "samples":        samples,
        "compliant_pct":  compliant_pct,
        "violations":     violations,
    }), 200


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
    # Production WSGI server (Flask's app.run dev server is single-threaded).
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=8)
