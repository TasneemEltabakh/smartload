-- SmartLoad Telemetry Schema for TimescaleDB
-- Issue #10: Deploy Time-Series Metrics Database
-- Version: 1.0 (FROZEN — see telemetry-v1.md for change policy)

-- ====================================================================
-- EXTENSIONS
-- ====================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ====================================================================
-- CORE HYPERTABLE: telemetry_metrics
-- Raw sub-minute telemetry from all SmartLoad nodes
-- ====================================================================

CREATE TABLE IF NOT EXISTS telemetry_metrics (
    time                TIMESTAMPTZ NOT NULL,

    -- Node identity (required, per telemetry-v1.md §4)
    service_name        TEXT NOT NULL,
    instance_id         TEXT NOT NULL,
    node_id             UUID NOT NULL,

    -- Required metrics (per telemetry-v1.md §5.1)
    smartload_request_latency_ms           FLOAT8,
    smartload_request_count                INT8,
    smartload_error_rate                   FLOAT8,

    -- Optional metrics (per telemetry-v1.md §5.2)
    smartload_backend_cpu_usage            FLOAT8,
    smartload_backend_memory_usage         FLOAT8,
    smartload_routing_decision_engine      TEXT,

    -- Derived / exporter metrics
    smartload_error_count_total                  INT8,
    smartload_backend_latency_ms                 FLOAT8,
    smartload_routing_backend_requests_total     INT8,

    -- Flexible attributes sidecar
    attributes          JSONB,

    -- Record metadata
    source              TEXT DEFAULT 'real',
    environment         TEXT DEFAULT 'development',
    created_at          TIMESTAMPTZ DEFAULT NOW(),

    PRIMARY KEY (time, node_id)
);

-- Convert to hypertable (1-day chunks = ~1 day of hot data per partition)
SELECT create_hypertable(
    'telemetry_metrics',
    'time',
    chunk_time_interval => interval '1 day',
    if_not_exists       => true
);

-- Compress data older than 1 day (keeps hot window uncompressed)
ALTER TABLE telemetry_metrics SET (
    timescaledb.compress,
    timescaledb.compress_orderby = 'time DESC',
    timescaledb.compress_segmentby = 'service_name, node_id'
);
SELECT add_compression_policy(
    'telemetry_metrics',
    compress_after => interval '1 day',
    if_not_exists  => true
);

-- Retention: keep 7 days of raw data (per telemetry-v1.md §7)
SELECT add_retention_policy(
    'telemetry_metrics',
    interval '7 days',
    if_not_exists => true
);

-- Query indexes
CREATE INDEX IF NOT EXISTS idx_telemetry_metrics_service_time
    ON telemetry_metrics (service_name, time DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_metrics_node_time
    ON telemetry_metrics (node_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_metrics_source_time
    ON telemetry_metrics (source, time DESC);

-- ====================================================================
-- ISSUE #10 EXPLICIT REQUIREMENT: lb_request_latencies view
-- "The database has at least one table to store LB request latencies"
-- This view exposes latency data in a named, queryable surface
-- ====================================================================

CREATE OR REPLACE VIEW lb_request_latencies AS
    SELECT
        time,
        service_name,
        instance_id,
        node_id,
        smartload_request_latency_ms    AS latency_ms,
        smartload_backend_latency_ms    AS backend_latency_ms,
        smartload_request_count         AS request_count,
        smartload_error_rate            AS error_rate,
        source,
        environment
    FROM telemetry_metrics
    WHERE smartload_request_latency_ms IS NOT NULL
       OR smartload_backend_latency_ms IS NOT NULL;

COMMENT ON VIEW lb_request_latencies IS
    'Convenience view: LB latency metrics from telemetry_metrics. Issue #10 acceptance criteria.';

-- ====================================================================
-- 1-MINUTE CONTINUOUS AGGREGATE (dashboards, alerting)
-- ====================================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_1min
WITH (timescaledb.continuous, timescaledb.materialized_only=false) AS
    SELECT
        time_bucket('1 minute', time) AS bucket,
        service_name,
        instance_id,
        node_id,
        source,
        environment,

        AVG(smartload_request_latency_ms)  AS avg_latency_ms,
        MAX(smartload_request_latency_ms)  AS max_latency_ms,
        MIN(smartload_request_latency_ms)  AS min_latency_ms,
        PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY smartload_request_latency_ms)
                                           AS p95_latency_ms,

        SUM(smartload_request_count)       AS total_requests,
        AVG(smartload_error_rate)          AS avg_error_rate,
        AVG(smartload_backend_cpu_usage)   AS avg_cpu,
        AVG(smartload_backend_memory_usage) AS avg_memory,

        COUNT(*)                           AS sample_count
    FROM telemetry_metrics
    GROUP BY bucket, service_name, instance_id, node_id, source, environment;

SELECT add_continuous_aggregate_policy(
    'telemetry_1min',
    start_offset    => interval '10 minutes',
    end_offset      => interval '2 minutes',
    schedule_interval => interval '1 minute',
    if_not_exists   => true
);

SELECT add_retention_policy(
    'telemetry_1min',
    interval '30 days',
    if_not_exists => true
);

-- ====================================================================
-- 1-HOUR CONTINUOUS AGGREGATE (ML training, long-term trends)
-- ====================================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_hourly
WITH (timescaledb.continuous, timescaledb.materialized_only=false) AS
    SELECT
        time_bucket('1 hour', time)         AS bucket,
        service_name,
        instance_id,
        node_id,
        source,
        environment,

        AVG(smartload_request_latency_ms)   AS avg_latency_ms,
        MAX(smartload_request_latency_ms)   AS max_latency_ms,
        SUM(smartload_request_count)        AS total_requests,
        AVG(smartload_error_rate)           AS avg_error_rate,
        AVG(smartload_backend_cpu_usage)    AS avg_cpu,
        AVG(smartload_backend_memory_usage) AS avg_memory,

        COUNT(*)                            AS sample_count
    FROM telemetry_metrics
    GROUP BY bucket, service_name, instance_id, node_id, source, environment;


SELECT add_retention_policy(
    'telemetry_hourly',
    interval '60 days',
    if_not_exists => true
);

-- ====================================================================
-- VALIDATION FAILURES TABLE (capture rejected records)
-- ====================================================================

CREATE TABLE IF NOT EXISTS telemetry_validation_failed (
    id              BIGSERIAL PRIMARY KEY,
    received_at     TIMESTAMPTZ NOT NULL,
    raw_payload     JSONB,
    rejection_reason TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_validation_failed_received_at
    ON telemetry_validation_failed (received_at DESC);


-- ====================================================================
-- BACKEND NODE REGISTRY
-- ====================================================================

CREATE TABLE IF NOT EXISTS backend_nodes (
    node_id      UUID PRIMARY KEY,
    service_name TEXT NOT NULL,
    instance_id  TEXT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    last_seen    TIMESTAMPTZ,
    is_active    BOOLEAN DEFAULT true
);

CREATE INDEX IF NOT EXISTS idx_backend_nodes_service_instance
    ON backend_nodes (service_name, instance_id);

-- ====================================================================
-- APP USER (uncomment to enable least-privilege access)
-- ====================================================================

-- DO $$
-- BEGIN
--     IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'smartload_app') THEN
--         CREATE ROLE smartload_app WITH LOGIN PASSWORD 'changeme_in_prod';
--     END IF;
-- END $$;
-- GRANT USAGE ON SCHEMA public TO smartload_app;
-- GRANT SELECT, INSERT ON telemetry_metrics TO smartload_app;
-- GRANT SELECT ON telemetry_1min, telemetry_hourly TO smartload_app;
-- GRANT SELECT ON lb_request_latencies TO smartload_app;
-- GRANT SELECT ON backend_nodes TO smartload_app;
-- GRANT INSERT ON telemetry_validation_failed TO smartload_app;