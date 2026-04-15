"""
SmartLoad TimescaleDB Ingester

Lightweight HTTP service that receives metrics from the OTel Collector
via Prometheus remote_write (snappy-compressed protobuf) and writes them
to TimescaleDB with validation against telemetry-v1.md schema.

Endpoints:
  POST /api/v1/write  - Ingest metrics (Prometheus remote_write format)
  POST /metrics       - Ingest metrics (legacy JSON format)
  GET  /health        - Health check
  GET  /stats         - Ingestion statistics
"""

import json
import os
import logging
import time
import struct
import snappy
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import threading


import psycopg2
from psycopg2.extras import execute_batch
from psycopg2 import pool as pg_pool   #added

# ============================================================================
# Configuration
# ============================================================================

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres123")
DB_NAME = os.getenv("DB_NAME", "smartload")
INGESTER_PORT = int(os.getenv("INGESTER_PORT", "5555"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="[%(levelname)s] %(asctime)s — %(message)s"
)
logger = logging.getLogger(__name__)

# ============================================================================
# Metrics State
# ============================================================================

class IngestionStats:
    def __init__(self):
        self.lock = threading.Lock()
        self.total_received = 0
        self.total_accepted = 0
        self.total_rejected = 0
        self.total_stored = 0
        self.last_error = None

    def increment_received(self):
        with self.lock:
            self.total_received += 1

    def increment_accepted(self):
        with self.lock:
            self.total_accepted += 1

    def increment_rejected(self, reason):
        with self.lock:
            self.total_rejected += 1
            self.last_error = reason

    def increment_stored(self):
        with self.lock:
            self.total_stored += 1

    def get_stats(self):
        with self.lock:
            return {
                "total_received": self.total_received,
                "total_accepted": self.total_accepted,
                "total_rejected": self.total_rejected,
                "total_stored": self.total_stored,
                "last_error": self.last_error,
            }

stats = IngestionStats()

# ============================================================================
# Minimal Protobuf Wire Parser for Prometheus WriteRequest
# Parses snappy-compressed protobuf sent by prometheusremotewrite exporter.
# No protobuf library needed - uses raw wire format (field tags + lengths).
#
# Proto schema being parsed:
#   WriteRequest  { repeated TimeSeries timeseries = 1 }
#   TimeSeries    { repeated Label labels = 1; repeated Sample samples = 2 }
#   Label         { string name = 1; string value = 2 }
#   Sample        { double value = 1; int64 timestamp_ms = 2 }
# ============================================================================

def _read_varint(data: bytes, pos: int):
    result, shift = 0, 0
    while True:
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7

def _read_length_delimited(data: bytes, pos: int):
    length, pos = _read_varint(data, pos)
    return data[pos:pos + length], pos + length

def _parse_label(data: bytes):
    pos, name, value = 0, None, None
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        field_number, wire_type = tag >> 3, tag & 0x7
        if wire_type == 2:
            val, pos = _read_length_delimited(data, pos)
            if field_number == 1:
                name = val.decode("utf-8")
            elif field_number == 2:
                value = val.decode("utf-8")
        else:
            break
    return name, value

def _parse_sample(data: bytes):
    pos, value, timestamp_ms = 0, None, None
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        field_number, wire_type = tag >> 3, tag & 0x7
        if field_number == 1 and wire_type == 1:    # double (64-bit little-endian)
            value = struct.unpack_from("<d", data, pos)[0]; pos += 8
        elif field_number == 2 and wire_type == 0:  # int64 varint
            timestamp_ms, pos = _read_varint(data, pos)
        else:
            break
    return value, timestamp_ms

def _parse_timeseries(data: bytes):
    pos, labels, samples = 0, {}, []
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        field_number, wire_type = tag >> 3, tag & 0x7
        if wire_type == 2:
            val, pos = _read_length_delimited(data, pos)
            if field_number == 1:    # Label
                name, value = _parse_label(val)
                if name and value is not None:
                    labels[name] = value
            elif field_number == 2:  # Sample
                v, ts = _parse_sample(val)
                if v is not None and ts is not None:
                    samples.append((v, ts))
        else:
            # Skip unknown wire types safely
            if wire_type == 0:
                _, pos = _read_varint(data, pos)
            elif wire_type == 1:
                pos += 8
            elif wire_type == 5:
                pos += 4
            else:
                break
    return labels, samples

def _parse_timeseries_block(data: bytes):
    pos, results = 0, []
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        field_number, wire_type = tag >> 3, tag & 0x7
        if field_number == 1 and wire_type == 2:  # repeated TimeSeries
            ts_data, pos = _read_length_delimited(data, pos)
            labels, samples = _parse_timeseries(ts_data)
            if labels and samples:
                results.append((labels, samples))
        else:
            if wire_type == 2:
                _, pos = _read_length_delimited(data, pos)
            elif wire_type == 0:
                _, pos = _read_varint(data, pos)
            elif wire_type == 1:
                pos += 8
            else:
                break
    return results

def remote_write_to_records(body: bytes) -> list:
    """
    Decompress snappy body, parse Prometheus WriteRequest protobuf,
    and return a list of telemetry-v1 compatible dicts ready for insert_record().
    Groups all metrics from the same (service_name, instance_id, node_id, timestamp)
    into one record so validation passes.
    """
    try:
        raw = snappy.decompress(body)
    except Exception as e:
        raise ValueError(f"Snappy decompression failed: {e}")

    time_series_list = _parse_timeseries_block(raw)

    # Group by (service_name, instance_id, node_id, timestamp_ms)
    # so all metrics for the same identity+time become one record
    groups = {}
    for labels, samples in time_series_list:
        metric_name = labels.get("__name__", "")
        service_name = labels.get("service_name", "nginx-lb")
        instance_id  = labels.get("instance_id",  "nginx-001")
        node_id      = labels.get("node_id",       "a0000000-0000-0000-0000-000000000001")
        source       = labels.get("source",        "real")
        environment  = labels.get("environment",   "development")

        for value, timestamp_ms in samples:
            # Convert ms timestamp to ISO 8601 UTC with millisecond precision
            ts = datetime.utcfromtimestamp(timestamp_ms / 1000.0)
            ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"

            key = (service_name, instance_id, node_id, ts_str)
            if key not in groups:
                groups[key] = {
                    "timestamp":    ts_str,
                    "service_name": service_name,
                    "instance_id":  instance_id,
                    "node_id":      node_id,
                    "metrics":      {},
                    "attributes": {
                        "source":      source,
                        "environment": environment,
                    },
                }

            # Map Prometheus metric name -> telemetry-v1 metric key
            name_map = {
                "smartload_request_count_total":                    "smartload_request_count",
                "smartload_request_latency_ms":                     "smartload_request_latency_ms",
                "smartload_request_latency_ms_sum":                 "smartload_request_latency_ms",
                "smartload_request_latency_ms_count":               "smartload_request_latency_ms",
                "smartload_request_latency_ms_bucket":              "smartload_request_latency_ms",
                "smartload_error_rate":                             "smartload_error_rate",
                "smartload_error_count_total":                      "smartload_error_count_total",
                "smartload_backend_latency_ms":                     "smartload_backend_latency_ms",
                "smartload_backend_latency_ms_sum":                 "smartload_backend_latency_ms",
                "smartload_backend_latency_ms_count":               "smartload_backend_latency_ms",
                "smartload_backend_latency_ms_bucket":              "smartload_backend_latency_ms",
                "smartload_routing_backend_requests_total":         "smartload_routing_backend_requests_total",
            }
            mapped = name_map.get(metric_name)
            if mapped:
                # Don't overwrite a value already set for this key
                if mapped not in groups[key]["metrics"]:
                    groups[key]["metrics"][mapped] = value

    return list(groups.values())

# ============================================================================
# Database Connection
# ============================================================================


# Initialize connection pool (min 2, max 10 connections)
_db_pool = None

def init_db_pool():
    """Initialize the connection pool at startup."""
    global _db_pool
    retries = 10
    for attempt in range(1, retries + 1):
        try:
            _db_pool = pg_pool.ThreadedConnectionPool(
                minconn=2,
                maxconn=10,
                host=DB_HOST,
                port=DB_PORT,
                user=DB_USER,
                password=DB_PASSWORD,
                database=DB_NAME,
                connect_timeout=5,
            )
            logger.info("Database connection pool initialized")
            return
        except Exception as e:
            logger.warning(f"DB pool init attempt {attempt}/{retries} failed: {e}")
            time.sleep(3)
    raise RuntimeError("Could not connect to database after retries")

def get_db_connection():
    """Acquire a connection from the pool."""
    if _db_pool is None:
        raise RuntimeError("Connection pool not initialized")
    return _db_pool.getconn()

def release_db_connection(conn):
    """Return a connection to the pool."""
    if _db_pool and conn:
        _db_pool.putconn(conn)

# ============================================================================
# Schema Validation (enforce telemetry-v1.md)
# ============================================================================

REQUIRED_FIELDS = ["timestamp", "service_name", "instance_id", "node_id"]

# Any record that has at least one of these is accepted.
# Broadened to cover all mapped metric names including histogram suffixes.
ACCEPTED_METRICS = {
    "smartload_request_latency_ms",
    "smartload_request_count",
    "smartload_error_rate",
    "smartload_error_count_total",
    "smartload_backend_latency_ms",
    "smartload_routing_backend_requests_total",
}

VALID_SOURCES = {
    "real", "synthetic", "borg", "alibaba", "azure",
    "planetlab", "bitbrains", "nab", "yahoo_smd"
}

def validate_record(record: dict) -> tuple[bool, str]:
    """Validate record against telemetry-v1.md schema."""

    # 1. Required identity fields
    for field in REQUIRED_FIELDS:
        if field not in record:
            return False, f"Missing required field: {field}"

    # 2. Timestamp format
    try:
        ts_str = record["timestamp"]
        if not ts_str.endswith("Z") or "T" not in ts_str:
            return False, f"Invalid timestamp format: {ts_str}"
        datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception as e:
        return False, f"Invalid timestamp: {e}"

    # 3. Metrics object must exist and be a dict
    if "metrics" not in record:
        return False, "Missing 'metrics' object"
    metrics = record["metrics"]
    if not isinstance(metrics, dict):
        return False, "Metrics must be an object"

    # 4. At least one recognized metric must be present and numeric
    found_any = False
    for metric_name, val in metrics.items():
        if metric_name in ACCEPTED_METRICS:
            if not isinstance(val, (int, float)):
                return False, f"Metric {metric_name} must be numeric, got {type(val)}"
            found_any = True

    if not found_any:
        return False, f"No recognized metric found. Must include at least one of: {sorted(ACCEPTED_METRICS)}"

    # 5. Source attribute validation (optional field)
    attrs = record.get("attributes", {})
    if "source" in attrs and attrs["source"] not in VALID_SOURCES:
        return False, f"Invalid source '{attrs['source']}'. Valid: {sorted(VALID_SOURCES)}"

    return True, ""

# ============================================================================
# Insert into Database
# ============================================================================

def insert_record(record: dict) -> bool:
    """Insert validated record into telemetry_metrics table."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        ts      = record["timestamp"]
        metrics = record.get("metrics", {})
        attrs   = record.get("attributes", {})

        query = """
            INSERT INTO telemetry_metrics (
                time,
                service_name,
                instance_id,
                node_id,
                smartload_request_latency_ms,
                smartload_request_count,
                smartload_error_rate,
                smartload_backend_cpu_usage,
                smartload_backend_memory_usage,
                smartload_error_count_total,
                smartload_backend_latency_ms,
                smartload_routing_backend_requests_total,
                source,
                environment,
                attributes
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (time, node_id) DO NOTHING
        """

        values = (
            ts,
            record["service_name"],
            record["instance_id"],
            record["node_id"],
            metrics.get("smartload_request_latency_ms"),
            metrics.get("smartload_request_count"),
            metrics.get("smartload_error_rate"),
            metrics.get("smartload_backend_cpu_usage"),
            metrics.get("smartload_backend_memory_usage"),
            metrics.get("smartload_error_count_total"),
            metrics.get("smartload_backend_latency_ms"),
            metrics.get("smartload_routing_backend_requests_total"),
            attrs.get("source", "real"),
            attrs.get("environment", "development"),
            json.dumps(attrs) if attrs else None,
        )

        cursor.execute(query, values)
        conn.commit()
        stats.increment_stored()
        return True

    except Exception as e:
        logger.error(f"Insert failed: {e}")
        if conn:
            conn.rollback()
        return False

    finally:
        if cursor:
            cursor.close()
        release_db_connection(conn)

def insert_validation_failure(raw_payload: dict, reason: str) -> None:
    """Persist rejected records to telemetry_validation_failed for debugging."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO telemetry_validation_failed (received_at, raw_payload, rejection_reason)
            VALUES (NOW(), %s, %s)
            """,
            (json.dumps(raw_payload), reason),
        )
        conn.commit()
        cursor.close()
    except Exception as e:
        logger.error(f"Failed to write validation failure record: {e}")
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)
# ============================================================================
# HTTP Request Handler
# ============================================================================

class MetricsHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        """Override to use project logger."""
        logger.info(f"{self.client_address[0]} — {format % args}")

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            response = {
                "status": "healthy",
                "service": "timescaledb-ingester",
                "uptime_seconds": int(time.time()),
            }
            self.wfile.write(json.dumps(response).encode())

        elif path == "/stats":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(stats.get_stats()).encode())

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path

        # ----------------------------------------------------------------
        # Prometheus remote_write endpoint (prometheusremotewrite exporter)
        # Body: snappy-compressed protobuf WriteRequest
        # ----------------------------------------------------------------
        if path == "/api/v1/write":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)

            try:
                records = remote_write_to_records(body)
                stored = 0
                rejected = 0

                for record in records:
                    stats.increment_received()
                    is_valid, reason = validate_record(record)
                    if not is_valid:
                        logger.debug(f"remote_write record skipped: {reason}")
                        stats.increment_rejected(reason)
                        insert_validation_failure(record, reason)
                        rejected += 1
                        continue
                    stats.increment_accepted()
                    if insert_record(record):
                        stored += 1
                    else:
                        rejected += 1

                logger.info(f"remote_write batch: {stored} stored, {rejected} skipped")
                # Prometheus remote_write spec requires 204 on success
                self.send_response(204)
                self.end_headers()

            except ValueError as e:
                logger.error(f"remote_write parse error: {e}")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())

            except Exception as e:
                logger.error(f"remote_write unexpected error: {e}")
                self.send_response(500)
                self.end_headers()

        # ----------------------------------------------------------------
        # Legacy JSON endpoint (kept for manual testing / curl)
        # Body: telemetry-v1 JSON record
        # ----------------------------------------------------------------
        elif path == "/metrics":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)

            try:
                record = json.loads(body.decode())
                stats.increment_received()

                is_valid, reason = validate_record(record)
                if not is_valid:
                    logger.warning(f"Validation failed: {reason}")
                    stats.increment_rejected(reason)
                    insert_validation_failure(record, reason)
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": reason}).encode())
                    return

                stats.increment_accepted()

                if insert_record(record):
                    self.send_response(201)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "stored"}).encode())
                else:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "failed to insert"}).encode())

            except json.JSONDecodeError as e:
                logger.error(f"JSON decode failed: {e}")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "invalid JSON"}).encode())

            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                self.send_response(500)
                self.end_headers()

        else:
            self.send_response(404)
            self.end_headers()

# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    logger.info(f"Starting TimescaleDB Ingester on 0.0.0.0:{INGESTER_PORT}")
    logger.info(f"Database: {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")

    init_db_pool()

    server = HTTPServer(("0.0.0.0", INGESTER_PORT), MetricsHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.shutdown()