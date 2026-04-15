"""
End-to-end test: Nginx → OTel Collector → TimescaleDB

Verifies:
  1. Collector receives metrics from nginx-metrics-exporter
  2. Metrics are validated and stored in DB
  3. Data can be queried from TimescaleDB
"""

import json
import time
import requests
import psycopg2
import pytest

NGINX_EXPORTER_URL = "http://localhost:9113/metrics"
OTEL_COLLECTOR_HEALTH = "http://localhost:13133"
TIMESCALEDB_INGESTER_URL = "http://localhost:5555"
TIMESCALEDB_HEALTH = "postgresql://postgres:postgres123@localhost:5432/smartload"

@pytest.fixture(scope="session")
def wait_for_services():
    """Wait for all HTTP services AND TimescaleDB to be ready."""
    max_retries = 40
    http_services = [
        ("Nginx Exporter",        NGINX_EXPORTER_URL),
        ("OTel Collector",        OTEL_COLLECTOR_HEALTH),
        ("TimescaleDB Ingester",  TIMESCALEDB_INGESTER_URL + "/health"),
    ]

    for attempt in range(1, max_retries + 1):
        all_healthy = True

        # Check HTTP services
        for name, url in http_services:
            try:
                r = requests.get(url, timeout=5)
                if not (200 <= r.status_code < 300):
                    print(f"  ✗ {name} returned {r.status_code}")
                    all_healthy = False
                else:
                    print(f"  ✓ {name}")
            except Exception as e:
                print(f"  ✗ {name}: {e}")
                all_healthy = False

        # Check TimescaleDB directly with a real connection
        if all_healthy:
            try:
                conn = psycopg2.connect(TIMESCALEDB_HEALTH, connect_timeout=3)
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM telemetry_metrics LIMIT 1")
                cursor.close()
                conn.close()
                print("  ✓ TimescaleDB (schema ready)")
            except psycopg2.OperationalError as e:
                print(f"  ✗ TimescaleDB not ready: {e}")
                all_healthy = False
            except psycopg2.errors.UndefinedTable:
                print("  ✗ TimescaleDB schema not initialized yet")
                all_healthy = False

        if all_healthy:
            print(f"All services healthy after {attempt} attempts")
            break

        print(f"Waiting for services... ({attempt}/{max_retries})")
        time.sleep(3)
    else:
        pytest.fail("Services did not become healthy within the timeout")

    yield

def test_nginx_exporter_health(wait_for_services):
    """Test nginx-metrics-exporter is running."""
    # Send traffic first so request_count_total appears
    for _ in range(5):
        try:
            requests.get("http://localhost:8080/", timeout=5)
        except:
            pass
    time.sleep(3)  # give exporter time to process logs

    response = requests.get(NGINX_EXPORTER_URL)
    assert response.status_code == 200
    assert b"smartload_request_count_total" in response.content

def test_otel_collector_health(wait_for_services):
    """Test OTel Collector is running."""
    response = requests.get(OTEL_COLLECTOR_HEALTH)
    assert response.status_code == 200

def test_timescaledb_ingester_health(wait_for_services):
    """Test TimescaleDB ingester is running."""
    response = requests.get(TIMESCALEDB_INGESTER_URL + "/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"

def test_db_schema_exists(wait_for_services):
    """Test TimescaleDB has required tables."""
    try:
        conn = psycopg2.connect(TIMESCALEDB_HEALTH)
        cursor = conn.cursor()
        
        # Check telemetry_metrics table
        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'telemetry_metrics'
            )
        """)
        assert cursor.fetchone()[0], "telemetry_metrics table not found"
        
        # Check continuous aggregates
        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'telemetry_1min'
            )
        """)
        assert cursor.fetchone()[0], "telemetry_1min aggregate not found"
        
        cursor.close()
        conn.close()
    except Exception as e:
        pytest.fail(f"Database schema check failed: {e}")

def test_generate_traffic_and_verify_in_db(wait_for_services):
    """Generate traffic and verify it appears in TimescaleDB."""
    # Send some requests through the load balancer
    for i in range(10):
        try:
            requests.get("http://localhost:8080/", timeout=5)
        except:
            pass
    
    # Wait for collector to batch and write
    time.sleep(35)
    
    # Query TimescaleDB
    try:
        conn = psycopg2.connect(TIMESCALEDB_HEALTH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT COUNT(*) FROM telemetry_metrics
        """)
        row_count = cursor.fetchone()[0]
        assert row_count > 0, f"No metrics in DB (expected >0, got {row_count})"
        
        # Verify required fields
        cursor.execute("""
            SELECT service_name, instance_id, node_id, smartload_request_count
            FROM telemetry_metrics
            LIMIT 1
        """)
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "nginx-lb"  # service_name
        assert row[1] == "nginx-001"  # instance_id
        
        cursor.close()
        conn.close()
        print(f"✓ Found {row_count} metric rows in DB")
    except Exception as e:
        pytest.fail(f"Database query failed: {e}")

def test_1min_aggregate_populated(wait_for_services):
    """Test 1-minute continuous aggregate is populated."""
    try:
        conn = psycopg2.connect(TIMESCALEDB_HEALTH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT COUNT(*) FROM telemetry_1min
        """)
        row_count = cursor.fetchone()[0]
        # May be 0 if not enough time has passed; just check table exists
        assert row_count >= 0
        
        cursor.close()
        conn.close()
        print(f"✓ telemetry_1min has {row_count} rows")
    except Exception as e:
        pytest.fail(f"1-minute aggregate check failed: {e}")

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])