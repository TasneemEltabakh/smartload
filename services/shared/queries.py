"""
services/shared/queries.py
──────────────────────────
TimescaleDB SQL query constants for each AI service.

These are the canonical queries that define the exact columns and data shapes
each service expects from the database. They were written BEFORE the schema
was finalised (N0.2), so the schema in infrastructure/timescaledb/init.sql
is guaranteed to satisfy them.

Usage:
    from services.shared.queries import ANOMALY_QUERY
    cursor.execute(ANOMALY_QUERY.format(window="60 seconds"))
"""

# ── anomaly-detector ──────────────────────────────────────────────────────────
# Fetches per-backend latency and error rate for the last N seconds.
# Returns one row per (instance, metric_name) pair.
ANOMALY_QUERY = """
SELECT
    instance,
    metric_name,
    AVG(value)  AS avg_value,
    MAX(value)  AS max_value,
    STDDEV(value) AS std_value,
    COUNT(*)    AS sample_count
FROM metrics
WHERE
    time > NOW() - INTERVAL '{window}'
    AND service = 'load-balancer'
    AND metric_name IN ('request_latency_ms', 'error_rate')
GROUP BY instance, metric_name
ORDER BY instance, metric_name;
"""

# ── forecasting ───────────────────────────────────────────────────────────────
# Fetches request-rate time series bucketed by minute for the last M hours.
# Returns one row per minute bucket — used to fit ARIMA / Prophet / moving avg.
FORECAST_QUERY = """
SELECT
    time_bucket('1 minute', time) AS bucket,
    SUM(value)                    AS request_rate
FROM metrics
WHERE
    time > NOW() - INTERVAL '{window}'
    AND metric_name = 'request_count'
GROUP BY bucket
ORDER BY bucket ASC;
"""

# ── rl-engine ─────────────────────────────────────────────────────────────────
# Fetches the current system state per backend (last 30 seconds).
# Returns one row per backend instance — used to build the RL state vector.
RL_STATE_QUERY = """
SELECT
    instance,
    AVG(CASE WHEN metric_name = 'request_latency_ms' THEN value END) AS latency,
    SUM(CASE WHEN metric_name = 'request_count'      THEN value END) AS request_count,
    MAX(CASE WHEN metric_name = 'error_rate'         THEN value END) AS error_rate
FROM metrics
WHERE time > NOW() - INTERVAL '30 seconds'
GROUP BY instance
ORDER BY instance;
"""

# ── backend health read ───────────────────────────────────────────────────────
# Latest health record per backend, used by rl-engine and load-balancer sidecar.
BACKEND_HEALTH_QUERY = """
SELECT DISTINCT ON (backend_id)
    backend_id,
    status,
    score,
    time
FROM backend_health
WHERE time > NOW() - INTERVAL '60 seconds'
ORDER BY backend_id, time DESC;
"""

# ── telemetry write helper ────────────────────────────────────────────────────
# Parameterised INSERT used by the telemetry service.
METRICS_INSERT = """
INSERT INTO metrics (time, service, instance, metric_name, value)
VALUES (%s, %s, %s, %s, %s);
"""

BACKEND_HEALTH_INSERT = """
INSERT INTO backend_health (time, backend_id, status, score)
VALUES (%s, %s, %s, %s);
"""

SCALING_EVENT_INSERT = """
INSERT INTO scaling_events (time, action, instance_count, reason)
VALUES (%s, %s, %s, %s);
"""

# ── schema verification ───────────────────────────────────────────────────────
# Used by integration tests to confirm required tables exist.
TABLE_EXISTS_QUERY = """
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name = ANY(%s);
"""

REQUIRED_TABLES = ["metrics", "backend_health", "scaling_events"]
