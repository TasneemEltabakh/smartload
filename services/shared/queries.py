"""
services/shared/queries.py
──────────────────────────
TimescaleDB SQL query constants for each AI service.

These are the canonical queries that define the exact columns and data shapes
each service expects from the database. They were written BEFORE the schema
was finalised (N0.2), so the schema in infrastructure/timescaledb/init.sql
is guaranteed to satisfy them.

Parameterization rule (SOT §11):
    All dynamic values — including time intervals — are passed as bind
    parameters, never via Python string formatting. PostgreSQL caches the
    prepared plan only when the query text is stable, and parameterised
    intervals stay safe even when the values originate from policy YAML.

Usage:
    from services.shared.queries import ANOMALY_QUERY
    cursor.execute(
        ANOMALY_QUERY,
        ("60 seconds", "load-balancer", ["request_latency_ms", "error_rate"]),
    )
"""

# ── anomaly-detector ──────────────────────────────────────────────────────────
# Fetches per-backend latency and error rate over a parameterised window.
# Returns one row per (instance, metric_name) pair.
#
# Parameters:
#   $1 window       — text interval, e.g. "60 seconds" (cast to interval in SQL)
#   $2 service      — text, e.g. "load-balancer"
#   $3 metric_names — text[], e.g. ["request_latency_ms", "error_rate"]
#
# The service filter is parameterised (not hardcoded to 'load-balancer') so the
# same engine can reason about backend-emitted telemetry once backends start
# emitting OTLP directly. SOT §11 "Canonical service filter rule".
ANOMALY_QUERY = """
SELECT
    instance,
    metric_name,
    AVG(value)    AS avg_value,
    MAX(value)    AS max_value,
    STDDEV(value) AS std_value,
    COUNT(*)      AS sample_count
FROM metrics
WHERE
    time > NOW() - %s::interval
    AND service = %s
    AND metric_name = ANY(%s)
GROUP BY instance, metric_name
ORDER BY instance, metric_name;
"""

# ── forecasting ───────────────────────────────────────────────────────────────
# Fetches request-rate time series bucketed by minute over a parameterised
# window. Returns one row per minute bucket — used to fit ARIMA / Prophet /
# moving avg.
#
# Parameters:
#   $1 window — text interval, e.g. "1 hour"
FORECAST_QUERY = """
SELECT
    time_bucket('1 minute', time) AS bucket,
    SUM(value)                    AS request_rate
FROM metrics
WHERE
    time > NOW() - %s::interval
    AND metric_name = 'request_count'
GROUP BY bucket
ORDER BY bucket ASC;
"""

# ── rl-engine ─────────────────────────────────────────────────────────────────
# Fetches the current system state per backend over a parameterised window
# (typical: "30 seconds"). Returns one row per backend instance — used to
# build the RL state vector.
#
# Parameters:
#   $1 window — text interval, e.g. "30 seconds"
RL_STATE_QUERY = """
SELECT
    instance,
    AVG(CASE WHEN metric_name = 'request_latency_ms' THEN value END) AS latency,
    SUM(CASE WHEN metric_name = 'request_count'      THEN value END) AS request_count,
    MAX(CASE WHEN metric_name = 'error_rate'         THEN value END) AS error_rate
FROM metrics
WHERE time > NOW() - %s::interval
GROUP BY instance
ORDER BY instance;
"""

# ── backend health read ───────────────────────────────────────────────────────
# Latest health record per backend over a parameterised window, used by
# rl-engine and load-balancer sidecar.
#
# Parameters:
#   $1 window — text interval, e.g. "60 seconds"
BACKEND_HEALTH_QUERY = """
SELECT DISTINCT ON (backend_id)
    backend_id,
    status,
    score,
    time
FROM backend_health
WHERE time > NOW() - %s::interval
ORDER BY backend_id, time DESC;
"""

# ── telemetry write helpers ───────────────────────────────────────────────────
# Single-row insert used by the telemetry service. For high write rates, prefer
# psycopg2.extras.execute_batch with this same statement.
METRICS_INSERT = """
INSERT INTO metrics (time, service, instance, metric_name, value)
VALUES (%s, %s, %s, %s, %s);
"""

# Multi-row insert helper for high write rates (preferred above ~50 rps).
# Per SOT §11 canonical SQL surface. Compose the VALUES list at the call site
# using psycopg2.extras.execute_values to keep the query plan stable.
METRICS_BATCH_INSERT = """
INSERT INTO metrics (time, service, instance, metric_name, value)
VALUES %s;
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

# ── canonical defaults for ANOMALY_QUERY callers ──────────────────────────────
# Engines should pass these as the third bind parameter unless they have a
# specific reason to query a different metric set.
ANOMALY_METRIC_NAMES = ["request_latency_ms", "error_rate"]
ANOMALY_DEFAULT_SERVICE = "load-balancer"
