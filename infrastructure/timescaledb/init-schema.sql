-- SmartLoad Telemetry Schema for TimescaleDB
-- Implements telemetry-v1.md with hypertables and continuous aggregates
-- Version: 1.0

-- ====================================================================
-- SETUP: Create extension and database
-- ====================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ====================================================================
-- CORE HYPERTABLE: telemetry_metrics (raw, sub-minute granularity)
-- ====================================================================

CREATE TABLE IF NOT EXISTS telemetry_metrics (
    time                TIMESTAMPTZ NOT NULL,
    
    -- Server identification (required)
    service_name        TEXT NOT NULL,
    instance_id         TEXT NOT NULL,
    node_id             UUID NOT NULL,
    
    -- Required metrics (per telemetry-v1.md)
    smartload_request_latency_ms FLOAT8,
    smartload_request_count      INT8,
    smartload_error_rate         FLOAT8,
    
    -- Optional metrics (per telemetry-v1.md)
    smartload_backend_cpu_usage     FLOAT8,
    smartload_backend_memory_usage  FLOAT8,
    smartload_routing_decision_engine TEXT,
    
    -- Additional derived metrics from exporter
    smartload_error_count_total       INT8,
    smartload_backend_latency_ms      FLOAT8,
    smartload_routing_backend_requests_total INT8,
    
    -- Attributes (optional, stored as JSONB for flexibility)
    attributes JSONB,
    
    -- Metadata
    created_at TIMESTAMPTZ DEFAULT NOW(),
    source TEXT DEFAULT 'real',
    environment TEXT DEFAULT 'development',
    
    PRIMARY KEY (time, node_id)
);

-- Convert to hypertable with 1-day chunks and node_id space partitioning
SELECT create_hypertable(
    'telemetry_metrics',
    'time',
    chunk_time_interval => interval '1 day',
    if_not_exists => true
);

-- Enable compression (keep last 1 day hot, compress older data)
SELECT add_compression_policy(
    'telemetry_metrics',
    compress_after => interval '1 day',
    if_not_exists => true
);

-- Set retention (raw metrics: 7 days per schema)
SELECT add_retention_policy(
    'telemetry_metrics',
    interval '7 days',
    if_not_exists => true
);

-- Create indexes for common queries
CREATE INDEX IF NOT EXISTS idx_telemetry_metrics_service_time 
    ON telemetry_metrics (service_name, time DESC);

CREATE INDEX IF NOT EXISTS idx_telemetry_metrics_node_time 
    ON telemetry_metrics (node_id, time DESC);

CREATE INDEX IF NOT EXISTS idx_telemetry_metrics_source_time 
    ON telemetry_metrics (source, time DESC);

-- ====================================================================
-- 1-MINUTE CONTINUOUS AGGREGATE (dashboard, alerting)
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
        
        -- Aggregated metrics
        AVG(smartload_request_latency_ms) AS avg_latency_ms,
        MAX(smartload_request_latency_ms) AS max_latency_ms,
        MIN(smartload_request_latency_ms) AS min_latency_ms,
        
        SUM(smartload_request_count) AS total_requests,
        AVG(smartload_error_rate) AS avg_error_rate,
        
        AVG(smartload_backend_cpu_usage) AS avg_cpu,
        AVG(smartload_backend_memory_usage) AS avg_memory,
        
        COUNT(*) AS sample_count
    FROM telemetry_metrics
    GROUP BY bucket, service_name, instance_id, node_id, source, environment;

-- Refresh policy for 1-min aggregate (every 1 min, lag 1 min)
SELECT add_continuous_aggregate_policy(
    'telemetry_1min',
    start_offset => interval '2 minutes',
    end_offset => interval '1 minute',
    schedule_interval => interval '1 minute',
    if_not_exists => true
);

-- Retention for 1-min: 30 days per schema
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
        time_bucket('1 hour', time) AS bucket,
        service_name,
        instance_id,
        node_id,
        source,
        environment,
        
        -- Aggregated metrics
        AVG(smartload_request_latency_ms) AS avg_latency_ms,
        MAX(smartload_request_latency_ms) AS max_latency_ms,
        
        SUM(smartload_request_count) AS total_requests,
        AVG(smartload_error_rate) AS avg_error_rate,
        AVG(smartload_backend_cpu_usage) AS avg_cpu,
        AVG(smartload_backend_memory_usage) AS avg_memory,
        
        COUNT(*) AS sample_count
    FROM telemetry_metrics
    GROUP BY bucket, service_name, instance_id, node_id, source, environment;

-- Refresh policy for 1-hour aggregate
SELECT add_continuous_aggregate_policy(
    'telemetry_hourly',
    start_offset => interval '2 hours',
    end_offset => interval '1 hour',
    schedule_interval => interval '1 hour',
    if_not_exists => true
);

-- Retention for hourly: 60 days per schema
SELECT add_retention_policy(
    'telemetry_hourly',
    interval '60 days',
    if_not_exists => true
);

-- ====================================================================
-- VALIDATION TABLE (capture rejected records)
-- ====================================================================

CREATE TABLE IF NOT EXISTS telemetry_validation_failed (
    id BIGSERIAL PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL,
    raw_payload JSONB,
    rejection_reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_validation_failed_received_at 
    ON telemetry_validation_failed (received_at DESC);

-- Retention for validation table: 30 days
SELECT add_retention_policy(
    'telemetry_validation_failed',
    interval '30 days',
    if_not_exists => true
);

-- ====================================================================
-- NODE REGISTRY TABLE (track registered nodes)
-- ====================================================================

CREATE TABLE IF NOT EXISTS backend_nodes (
    node_id UUID PRIMARY KEY,
    service_name TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_seen TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT true
);

CREATE INDEX IF NOT EXISTS idx_backend_nodes_service_instance 
    ON backend_nodes (service_name, instance_id);

-- ====================================================================
-- GRANT PERMISSIONS (if using dedicated app user)
-- ====================================================================

-- Create app user (for collector to write data)
-- DO $$
-- BEGIN
--     IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'smartload_app') THEN
--         CREATE ROLE smartload_app WITH LOGIN PASSWORD 'changeme';
--     END IF;
-- END $$;

-- GRANT USAGE ON SCHEMA public TO smartload_app;
-- GRANT ALL ON telemetry_metrics TO smartload_app;
-- GRANT ALL ON telemetry_1min TO smartload_app;
-- GRANT ALL ON telemetry_hourly TO smartload_app;
-- GRANT SELECT ON backend_nodes TO smartload_app;