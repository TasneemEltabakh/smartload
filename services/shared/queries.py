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
    SUM(value)::float / 60.0      AS request_rate
FROM metrics
WHERE
    time > NOW() - %s::interval
    AND metric_name = 'request_count'
GROUP BY bucket
ORDER BY bucket ASC;
"""

# ── forecasting history read ───────────────────────────────────────────────────
# Recent forecast rows over a parameterised window, newest first, for the
# operator-facing GET /api/v1/forecasts read endpoint. Bounded by an integer
# limit parameter capped at the route layer to avoid unbounded reads.
#
# Parameters (in order):
#   $1 window — text interval, e.g. "3600 seconds"
#   $2 limit  — int row cap
FORECAST_HISTORY_QUERY = """
SELECT
    time,
    horizon_minutes,
    predicted_rps,
    confidence_lower,
    confidence_upper,
    model_name,
    model_version
FROM forecasts
WHERE time > NOW() - %s::interval
ORDER BY time DESC
LIMIT %s;
"""

# ── rl-engine ─────────────────────────────────────────────────────────────────
# Fetches the current system state per backend over a parameterised window
# (typical: "30 seconds"). Returns one row per backend instance — used to
# build the RL state vector.
#
# Parameters:
#   $1 window   — text interval, e.g. "30 seconds"
#   $2 service  — text, e.g. "load-balancer"  (use RL_DEFAULT_SERVICE)
#
# error_rate uses AVG (not MAX): lb-otel-shipper emits 0.0 or 1.0 per request,
# so MAX would return binary 0/1 and over-classify backends as unhealthy on any
# single error. AVG yields the true decimal error fraction over the window.
#
# service filter matches ANOMALY_QUERY pattern (SOT §11 "Canonical service
# filter rule") to prevent cross-service instance name collisions.
RL_STATE_QUERY = """
SELECT
    instance,
    AVG(CASE WHEN metric_name = 'request_latency_ms' THEN value END) AS latency,
    SUM(CASE WHEN metric_name = 'request_count'      THEN value END) AS request_count,
    AVG(CASE WHEN metric_name = 'error_rate'         THEN value END) AS error_rate
FROM metrics
WHERE time > NOW() - %s::interval
  AND service = %s
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

# ── anomaly verdict history read ───────────────────────────────────────────────
# Recent per-backend health verdict rows over a parameterised window, newest
# first, for the operator-facing GET /api/v1/anomaly/history read endpoint.
# Optionally filtered to a single backend_id. Bounded by an integer limit
# parameter capped at the route layer to avoid unbounded reads.
#
# The optional backend filter is applied with a NULL-guard: when $2 is NULL the
# predicate degenerates to TRUE so the same statement text serves both the
# all-backends and single-backend cases (a stable query plan, SOT §11).
#
# Parameters (in order):
#   $1 window     — text interval, e.g. "3600 seconds"
#   $2 backend_id — text or NULL; NULL means "all backends"
#   $3 limit      — int row cap
ANOMALY_HISTORY_QUERY = """
SELECT
    time,
    backend_id,
    status,
    score
FROM backend_health
WHERE time > NOW() - %s::interval
  AND (%s::text IS NULL OR backend_id = %s)
ORDER BY time DESC
LIMIT %s;
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

# ── forecasting persist write ─────────────────────────────────────────────────
# One row per inference cycle, regardless of safe_mode. Closes the SOT §35.8
# gap: prior to #159 forecasts were only published to Redis, leaving the
# Grafana predicted-vs-actual overlay sparse and forcing the autoscaler's
# reason text to be the durable record of what was predicted.
#
# Parameters (in order):
#   $1 time             — TIMESTAMPTZ, the publish moment (UTC)
#   $2 horizon_minutes  — INT, the forecast horizon
#   $3 predicted_rps    — FLOAT, the point estimate
#   $4 confidence_lower — FLOAT or NULL, lower bound if engine supplies one
#   $5 confidence_upper — FLOAT or NULL, upper bound if engine supplies one
#   $6 model_name       — TEXT, e.g. "moving_average", "arima"
#   $7 model_version    — TEXT or NULL, engine artifact version for trained models
FORECASTS_INSERT = """
INSERT INTO forecasts
    (time, horizon_minutes, predicted_rps, confidence_lower, confidence_upper, model_name, model_version)
VALUES (%s, %s, %s, %s, %s, %s, %s);
"""

# ── policy-manager audit write ────────────────────────────────────────────────
# One row per changed field per successful POST. old_value / new_value are
# JSON-encoded at the call site so the column can store any field type
# (string, number, bool, list) without per-type columns.
POLICY_CHANGE_INSERT = """
INSERT INTO policy_changes (time, policy_version, field, old_value, new_value, actor)
VALUES (%s, %s, %s, %s, %s, %s);
"""

# ── autoscaler audit read ─────────────────────────────────────────────────────
# Recent rows from scaling_events for the audit-log slice (#122). Newest first;
# bounded by an integer limit parameter. Used by GET /api/v1/audit/scaling.
#
# Parameters:
#   $1 limit  — int, capped at the route layer to avoid unbounded reads.
SCALING_AUDIT_QUERY = """
SELECT time, action, instance_count, reason
FROM scaling_events
ORDER BY time DESC
LIMIT %s;
"""

# ── autoscaler reactive fallback ──────────────────────────────────────────────
# Single-number request rate for the autoscaler when the forecast stream goes
# stale. Window is 60 s — large enough to smooth bursty single-second counts,
# small enough to react to a sustained shift within one cooldown cycle. SOT
# §8.8 "Failure / fallback behavior".
OBSERVED_RPS_QUERY = """
SELECT COALESCE(SUM(value), 0)::float / 60.0
FROM metrics
WHERE time > NOW() - INTERVAL '60 seconds'
  AND metric_name = 'request_count';
"""

# ── schema verification ───────────────────────────────────────────────────────
# Used by integration tests to confirm required tables exist.
TABLE_EXISTS_QUERY = """
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name = ANY(%s);
"""

REQUIRED_TABLES = ["metrics", "backend_health", "scaling_events", "policy_changes", "forecasts"]

# ── canonical defaults for ANOMALY_QUERY callers ──────────────────────────────
# Engines should pass these as the third bind parameter unless they have a
# specific reason to query a different metric set.
ANOMALY_METRIC_NAMES = ["request_latency_ms", "error_rate"]
ANOMALY_DEFAULT_SERVICE = "load-balancer"

# ── canonical default for RL_STATE_QUERY callers ──────────────────────────────
# Pass as the second bind parameter to RL_STATE_QUERY. Matches
# ANOMALY_DEFAULT_SERVICE so both engines reason about the same service.
RL_DEFAULT_SERVICE = "load-balancer"
