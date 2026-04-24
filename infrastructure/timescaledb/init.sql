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
    reason         TEXT                    -- e.g. "forecast predicted 450 rps"
);

SELECT create_hypertable('scaling_events', 'time', if_not_exists => TRUE);
