"""
tests/integration/test_timescaledb_issue10.py

Issue #10 Acceptance Criteria Test Suite
─────────────────────────────────────────
✅ A running TimescaleDB instance is available
✅ The database has at least one table to store LB request latencies
✅ A test insert (from ingester HTTP endpoint) succeeds
✅ Schema (SQL file) is verifiable programmatically

Run with:
    # All services up:
    docker compose -f infrastructure/docker-compose.yml up -d
    sleep 10
    pytest tests/integration/test_timescaledb_issue10.py -v
"""

import json
import time
import uuid
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import pytest
import requests

# ── Connection constants ──────────────────────────────────────────────────────
DB_DSN       = "postgresql://postgres:postgres123@localhost:5432/smartload"
INGESTER_URL = "http://localhost:5555"

# ── Helpers ───────────────────────────────────────────────────────────────────

def utc_now_ms() -> str:
    """Return current UTC time as ISO-8601 with millisecond precision."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def db_conn():
    return psycopg2.connect(DB_DSN, connect_timeout=5)


# ── Session-scoped fixture: wait for services ─────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def wait_for_timescaledb():
    """Block until TimescaleDB is ready and schema is initialized."""
    for attempt in range(30):
        try:
            conn = db_conn()
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM telemetry_metrics LIMIT 1")
            cur.close()
            conn.close()
            return   # ready
        except psycopg2.OperationalError:
            pass
        except psycopg2.errors.UndefinedTable:
            pass
        print(f"  waiting for TimescaleDB... ({attempt+1}/30)")
        time.sleep(3)
    pytest.fail("TimescaleDB did not become ready within 90 seconds")


# ── AC1: A running TimescaleDB instance is available ─────────────────────────

class TestTimescaleDBAvailability:

    def test_connection_succeeds(self):
        """AC1: Can connect to TimescaleDB on port 5432."""
        conn = db_conn()
        assert conn is not None
        conn.close()

    def test_timescaledb_extension_installed(self):
        """AC1: TimescaleDB extension is active in the smartload database."""
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("SELECT extname FROM pg_extension WHERE extname = 'timescaledb'")
        row = cur.fetchone()
        cur.close(); conn.close()
        assert row is not None, "timescaledb extension not found"
        assert row[0] == "timescaledb"

    def test_database_name_is_smartload(self):
        """AC1: Connected to the 'smartload' database."""
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("SELECT current_database()")
        db_name = cur.fetchone()[0]
        cur.close(); conn.close()
        assert db_name == "smartload"


# ── AC2: Table exists to store LB request latencies ──────────────────────────

class TestSchemaStructure:

    def test_telemetry_metrics_table_exists(self):
        """AC2: telemetry_metrics table is present."""
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = 'telemetry_metrics'
        """)
        assert cur.fetchone() is not None, "telemetry_metrics table missing"
        cur.close(); conn.close()

    def test_lb_request_latencies_view_exists(self):
        """AC2 (explicit): lb_request_latencies view exists for LB latency queries."""
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT viewname FROM pg_views
            WHERE schemaname = 'public' AND viewname = 'lb_request_latencies'
        """)
        assert cur.fetchone() is not None, "lb_request_latencies view missing"
        cur.close(); conn.close()

    def test_telemetry_metrics_is_hypertable(self):
        """AC2: telemetry_metrics is a TimescaleDB hypertable."""
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT hypertable_name
            FROM timescaledb_information.hypertables
            WHERE hypertable_name = 'telemetry_metrics'
        """)
        assert cur.fetchone() is not None, "telemetry_metrics is not a hypertable"
        cur.close(); conn.close()

    def test_required_columns_present(self):
        """AC2: All telemetry-v1.md required columns exist."""
        required = {
            "time", "service_name", "instance_id", "node_id",
            "smartload_request_latency_ms", "smartload_request_count",
            "smartload_error_rate", "source", "environment",
        }
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'telemetry_metrics'
        """)
        actual = {row[0] for row in cur.fetchall()}
        cur.close(); conn.close()
        missing = required - actual
        assert not missing, f"Missing columns: {missing}"

    def test_continuous_aggregate_1min_exists(self):
        """AC2: 1-minute continuous aggregate is configured."""
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT view_name FROM timescaledb_information.continuous_aggregates
            WHERE view_name = 'telemetry_1min'
        """)
        assert cur.fetchone() is not None, "telemetry_1min aggregate missing"
        cur.close(); conn.close()

    def test_continuous_aggregate_hourly_exists(self):
        """AC2: Hourly continuous aggregate is configured."""
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT view_name FROM timescaledb_information.continuous_aggregates
            WHERE view_name = 'telemetry_hourly'
        """)
        assert cur.fetchone() is not None, "telemetry_hourly aggregate missing"
        cur.close(); conn.close()

    def test_retention_policies_active(self):
        """AC2: Retention policies exist (raw=7d, 1min=30d, hourly=60d)."""
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM timescaledb_information.jobs
            WHERE proc_name = 'policy_retention'
        """)
        count = cur.fetchone()[0]
        cur.close(); conn.close()
        assert count >= 3, f"Expected ≥3 retention policies (raw+1min+hourly), got {count}"


# ── AC3: A test insert succeeds ───────────────────────────────────────────────

class TestInsert:
    """
    AC3: A test insert succeeds — tested via both:
      (a) direct psql / psycopg2 insert
      (b) HTTP POST to timescaledb-ingester (OTel Collector path)
    """

    def test_direct_psycopg2_insert(self):
        """AC3a: Direct INSERT into telemetry_metrics via psycopg2 succeeds."""
        test_node = str(uuid.uuid4())
        test_ts   = utc_now_ms()

        conn = db_conn()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO telemetry_metrics (
                time, service_name, instance_id, node_id,
                smartload_request_latency_ms, smartload_request_count,
                smartload_error_rate, source, environment
            ) VALUES (%s, %s, %s, %s::uuid, %s, %s, %s, %s, %s)
            ON CONFLICT (time, node_id) DO NOTHING
        """, (
            test_ts, "nginx-lb", "pytest-direct-001", test_node,
            38.5, 1, 0.0, "real", "development"
        ))
        conn.commit()

        # Verify the row landed
        cur.execute(
            "SELECT smartload_request_latency_ms FROM telemetry_metrics "
            "WHERE node_id = %s::uuid",
            (test_node,)
        )
        row = cur.fetchone()
        assert row is not None, "Inserted row not found"
        assert abs(row[0] - 38.5) < 0.01, f"Unexpected latency value: {row[0]}"

        # Verify lb_request_latencies view reflects it
        cur.execute(
            "SELECT latency_ms FROM lb_request_latencies WHERE node_id = %s::uuid",
            (test_node,)
        )
        view_row = cur.fetchone()
        assert view_row is not None, "Row not visible through lb_request_latencies view"

        # Cleanup
        cur.execute("DELETE FROM telemetry_metrics WHERE node_id = %s::uuid", (test_node,))
        conn.commit()
        cur.close(); conn.close()

    def test_http_ingester_insert(self):
        """AC3b: POST to /metrics ingester endpoint stores record in DB."""
        # Skip gracefully if ingester is not running
        try:
            r = requests.get(f"{INGESTER_URL}/health", timeout=3)
            if r.status_code != 200:
                pytest.skip("Ingester not healthy — skipping HTTP insert test")
        except requests.exceptions.ConnectionError:
            pytest.skip("Ingester not reachable — skipping HTTP insert test")

        test_instance = f"pytest-http-{uuid.uuid4().hex[:8]}"
        payload = {
            "timestamp":    utc_now_ms(),
            "service_name": "nginx-lb",
            "instance_id":  test_instance,
            "node_id":      "a0000000-0000-0000-0000-000000000077",
            "metrics": {
                "smartload_request_latency_ms": 72.1,
                "smartload_request_count":      1,
                "smartload_error_rate":         0.0,
            },
            "attributes": {"source": "real", "environment": "development"},
        }

        resp = requests.post(
            f"{INGESTER_URL}/metrics",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=10,
        )
        assert resp.status_code == 201, (
            f"Expected 201, got {resp.status_code}: {resp.text}"
        )

        # Verify row is in DB
        conn = db_conn()
        cur  = conn.cursor()
        cur.execute(
            "SELECT smartload_request_latency_ms FROM telemetry_metrics "
            "WHERE instance_id = %s ORDER BY time DESC LIMIT 1",
            (test_instance,)
        )
        row = cur.fetchone()
        assert row is not None, "HTTP-inserted row not found in DB"
        assert abs(row[0] - 72.1) < 0.01

        # Cleanup
        cur.execute(
            "DELETE FROM telemetry_metrics WHERE instance_id = %s",
            (test_instance,)
        )
        conn.commit()
        cur.close(); conn.close()

    def test_validation_failure_is_recorded(self):
        """AC3: Invalid records are rejected and logged to telemetry_validation_failed."""
        try:
            r = requests.get(f"{INGESTER_URL}/health", timeout=3)
            if r.status_code != 200:
                pytest.skip("Ingester not running")
        except requests.exceptions.ConnectionError:
            pytest.skip("Ingester not reachable")

        # Send a record missing 'metrics' — should be rejected
        bad_payload = {
            "timestamp":    utc_now_ms(),
            "service_name": "nginx-lb",
            "instance_id":  "bad-record",
            "node_id":      "a0000000-0000-0000-0000-000000000000",
            # 'metrics' key intentionally omitted
        }
        resp = requests.post(
            f"{INGESTER_URL}/metrics",
            headers={"Content-Type": "application/json"},
            data=json.dumps(bad_payload),
            timeout=10,
        )
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"

        conn = db_conn()
        cur  = conn.cursor()
        cur.execute(
            "SELECT rejection_reason FROM telemetry_validation_failed "
            "ORDER BY received_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        assert row is not None, "Validation failure not recorded in DB"
        assert "metrics" in row[0].lower() or "missing" in row[0].lower()
        cur.close(); conn.close()

    def test_ingester_stats_endpoint(self):
        """AC3: /stats endpoint reports ingestion counters."""
        try:
            r = requests.get(f"{INGESTER_URL}/stats", timeout=3)
        except requests.exceptions.ConnectionError:
            pytest.skip("Ingester not reachable")

        assert r.status_code == 200
        data = r.json()
        for key in ("total_received", "total_accepted", "total_rejected", "total_stored"):
            assert key in data, f"Missing stats key: {key}"
            assert isinstance(data[key], int)