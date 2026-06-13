-- SmartLoad TimescaleDB schema initialisation
-- Runs automatically on first container start via /docker-entrypoint-initdb.d/

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ── metrics ──────────────────────────────────────────────────────────────────
-- General-purpose time-series metric store.
-- Populated by the telemetry service from the OTel Collector pipeline.
-- Queried by: anomaly-detector, forecasting, rl-engine
CREATE TABLE IF NOT EXISTS metrics (
    time         TIMESTAMPTZ      NOT NULL,
    service      TEXT             NOT NULL,   -- e.g. "load-balancer"
    instance     TEXT             NOT NULL,   -- container / backend ID
    metric_name  TEXT             NOT NULL,   -- e.g. "request_latency_ms", "request_count", "error_rate"
    value        DOUBLE PRECISION NOT NULL
);

SELECT create_hypertable('metrics', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_metrics_service_instance
    ON metrics (service, instance, time DESC);

CREATE INDEX IF NOT EXISTS idx_metrics_metric_name
    ON metrics (metric_name, time DESC);

-- ── backend_health ───────────────────────────────────────────────────────────
-- Written by anomaly-detector after each evaluation cycle.
-- Read by load-balancer sidecar (T2.1) and rl-engine.
CREATE TABLE IF NOT EXISTS backend_health (
    time        TIMESTAMPTZ      NOT NULL,
    backend_id  TEXT             NOT NULL,   -- matches instance in metrics table
    status      TEXT             NOT NULL,   -- "healthy" | "degraded" | "unhealthy"
    score       DOUBLE PRECISION NOT NULL    -- anomaly score (higher = more anomalous)
);

SELECT create_hypertable('backend_health', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_backend_health_backend
    ON backend_health (backend_id, time DESC);

-- ── scaling_events ───────────────────────────────────────────────────────────
-- Written by autoscaler after each scale-out or scale-in action.
CREATE TABLE IF NOT EXISTS scaling_events (
    time           TIMESTAMPTZ NOT NULL,
    action         TEXT        NOT NULL,   -- "scale_out" | "scale_in"
    instance_count INT         NOT NULL,   -- resulting backend count after action
    reason         TEXT,                   -- e.g. "forecast predicted 450 rps"
    mechanism      TEXT                    -- "start" | "provision" | "stop" | "decommission" (#155); NULL for legacy rows. Kept in sync with migration 0001 for existing volumes.
);

SELECT create_hypertable('scaling_events', 'time', if_not_exists => TRUE);

-- ── forecasts ────────────────────────────────────────────────────────────────
-- Written by the forecasting service once per inference cycle, before the
-- ForecastResult envelope is published to smartload.forecast. One row per
-- cycle regardless of safe_mode (safe_mode gates the publish, not the
-- observational write — operators still need to see what the engine would
-- have predicted).
--
-- Read by: Grafana Forecast dashboard (predicted-vs-actual overlay),
-- adaptive-bench R2 prom_collector (forecast-quality joins on time),
-- post-hoc evaluation scripts.
CREATE TABLE IF NOT EXISTS forecasts (
    time             TIMESTAMPTZ      NOT NULL,
    horizon_minutes  INT              NOT NULL,
    predicted_rps    DOUBLE PRECISION NOT NULL,
    confidence_lower DOUBLE PRECISION,
    confidence_upper DOUBLE PRECISION,
    model_name       TEXT             NOT NULL,   -- "moving_average" | "arima" | ...
    model_version    TEXT                         -- engine artifact version; NULL for untyped baselines
);

SELECT create_hypertable('forecasts', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_forecasts_model
    ON forecasts (model_name, time DESC);

-- ── policy_changes ───────────────────────────────────────────────────────────
-- Written by policy-manager on each successful POST /api/v1/policy that
-- changes at least one field. One row per changed field — operators can
-- reconstruct full state diffs by joining on (time, actor).
--
-- Read by: operator dashboards, thesis post-hoc analysis. Engines do not
-- consult this table — the live state arrives via smartload.policy.
CREATE TABLE IF NOT EXISTS policy_changes (
    time            TIMESTAMPTZ NOT NULL,
    policy_version  INT         NOT NULL,   -- post-change version (monotonic)
    field           TEXT        NOT NULL,   -- e.g. "max_backends"
    old_value       TEXT,                   -- JSON-encoded previous value; NULL if newly set
    new_value       TEXT        NOT NULL,   -- JSON-encoded new value
    actor           TEXT        NOT NULL    -- X-Actor header, "anonymous" if absent
);

SELECT create_hypertable('policy_changes', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_policy_changes_field
    ON policy_changes (field, time DESC);

-- ── retention policies ───────────────────────────────────────────────────────
-- Bound storage growth per the canonical data classes (SOT §10).
-- These DROP chunks older than the retention window; aggregates live longer.
SELECT add_retention_policy('metrics',         INTERVAL '7 days',  if_not_exists => TRUE);
SELECT add_retention_policy('backend_health',  INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_retention_policy('scaling_events',  INTERVAL '90 days', if_not_exists => TRUE);
SELECT add_retention_policy('policy_changes',  INTERVAL '90 days', if_not_exists => TRUE);
SELECT add_retention_policy('forecasts',       INTERVAL '30 days', if_not_exists => TRUE);

-- ── continuous aggregate: metrics_1min ───────────────────────────────────────
-- Per-minute roll-up over the canonical long-format `metrics` table.
-- Powers Grafana panels (T2.2) and gives the forecasting engine a cheap
-- request-rate surface without scanning raw chunks.
--
-- Engines that need sub-minute resolution (anomaly-detector, RL state) MUST
-- continue to read raw `metrics` — this aggregate has 2-minute lag by design.
--
-- materialized_only = true: queries hit the materialised data only; recent
-- rows in the [-2m, now] window aren't visible via this view (use raw metrics).
--
-- P95 is intentionally NOT computed here. PERCENTILE_CONT is an ordered-set
-- aggregate and is rejected by TimescaleDB CAGGs (the materialized view would
-- fail to create). Dashboards and engines compute P95 on raw `metrics` over a
-- bounded window — cheap at SmartLoad scale.
CREATE MATERIALIZED VIEW IF NOT EXISTS metrics_1min
WITH (timescaledb.continuous, timescaledb.materialized_only = true) AS
SELECT
    time_bucket('1 minute', time)   AS bucket,
    service,
    instance,
    metric_name,
    AVG(value)                      AS avg_value,
    MAX(value)                      AS max_value,
    MIN(value)                      AS min_value,
    SUM(value)                      AS sum_value,
    STDDEV(value)                   AS stddev_value,
    COUNT(*)                        AS sample_count
FROM metrics
GROUP BY bucket, service, instance, metric_name
WITH NO DATA;

-- Refresh every minute, materialising the [-1h, -2m] window. The 2-minute
-- end_offset gives raw chunks time to settle before being aggregated.
SELECT add_continuous_aggregate_policy(
    'metrics_1min',
    start_offset      => INTERVAL '1 hour',
    end_offset        => INTERVAL '2 minutes',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists     => TRUE
);

-- Aggregate retention: 30 days. (Raw `metrics` retention is 7d above.)
SELECT add_retention_policy('metrics_1min', INTERVAL '30 days', if_not_exists => TRUE);
