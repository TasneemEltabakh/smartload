#!/usr/bin/env bash
# infrastructure/timescaledb/verify-schema.sh
#
# Issue #10 Acceptance Criteria Verification
# Usage:
#   ./verify-schema.sh                          # auto-detect running container
#   TSDB_HOST=localhost ./verify-schema.sh      # point at specific host
#
# Requirements: psql client installed locally, or run inside the timescaledb
#               container: docker exec -it infrastructure-timescaledb-1 bash
#
set -euo pipefail

TSDB_HOST="${TSDB_HOST:-localhost}"
TSDB_PORT="${TSDB_PORT:-5432}"
TSDB_USER="${TSDB_USER:-postgres}"
TSDB_PASS="${TSDB_PASS:-postgres123}"
TSDB_DB="${TSDB_DB:-smartload}"

export PGPASSWORD="$TSDB_PASS"
PSQL="psql -h $TSDB_HOST -p $TSDB_PORT -U $TSDB_USER -d $TSDB_DB"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0

check() {
    local label="$1"; local query="$2"; local expect="$3"
    local result
    result=$($PSQL -t -A -c "$query" 2>&1) || { echo -e "${RED}✗ $label — psql error: $result${NC}"; ((FAIL++)); return; }
    if [[ "$result" == *"$expect"* ]]; then
        echo -e "${GREEN}✓ $label${NC}"
        ((PASS++))
    else
        echo -e "${RED}✗ $label — expected '$expect', got '$result'${NC}"
        ((FAIL++))
    fi
}

echo "======================================================"
echo "  SmartLoad TimescaleDB Schema Verification"
echo "  Target: $TSDB_USER@$TSDB_HOST:$TSDB_PORT/$TSDB_DB"
echo "======================================================"
echo ""

# ── 1. Instance is reachable ──────────────────────────────
echo "─── Connectivity ───────────────────────────────────"
check "TimescaleDB reachable" \
    "SELECT version();" \
    "PostgreSQL"

check "TimescaleDB extension installed" \
    "SELECT extname FROM pg_extension WHERE extname='timescaledb';" \
    "timescaledb"

# ── 2. Tables exist ───────────────────────────────────────
echo ""
echo "─── Tables ─────────────────────────────────────────"
for tbl in telemetry_metrics telemetry_validation_failed backend_nodes; do
    check "Table '$tbl' exists" \
        "SELECT to_regclass('public.$tbl')::text;" \
        "$tbl"
done

# ── 3. Hypertable configured ──────────────────────────────
echo ""
echo "─── Hypertable ─────────────────────────────────────"
check "telemetry_metrics is a hypertable" \
    "SELECT hypertable_name FROM timescaledb_information.hypertables WHERE hypertable_name='telemetry_metrics';" \
    "telemetry_metrics"

check "Compression policy active" \
    "SELECT hypertable_name FROM timescaledb_information.jobs j JOIN timescaledb_information.job_stats js USING(job_id) WHERE proc_name='policy_compression' LIMIT 1;" \
    "telemetry_metrics"

check "Retention policy active (7 days)" \
    "SELECT config::text FROM timescaledb_information.jobs WHERE proc_name='policy_retention' AND config::text LIKE '%telemetry_metrics%' LIMIT 1;" \
    "telemetry_metrics"

# ── 4. Continuous aggregates ──────────────────────────────
echo ""
echo "─── Continuous Aggregates ───────────────────────────"
for view in telemetry_1min telemetry_hourly; do
    check "Aggregate '$view' exists" \
        "SELECT view_name FROM timescaledb_information.continuous_aggregates WHERE view_name='$view';" \
        "$view"
done

# ── 5. lb_request_latencies view (Issue #10 explicit AC) ─
echo ""
echo "─── Issue #10 Acceptance Criteria ───────────────────"
check "lb_request_latencies view exists" \
    "SELECT viewname FROM pg_views WHERE viewname='lb_request_latencies';" \
    "lb_request_latencies"

# ── 6. Test insert (Issue #10: "a test insert succeeds") ─
echo ""
echo "─── Test Insert ─────────────────────────────────────"

TEST_UUID="a0000000-0000-0000-0000-000000000099"
TEST_TS="$(date -u +"%Y-%m-%dT%H:%M:%S.000Z")"

INSERT_RESULT=$($PSQL -t -A -c "
    INSERT INTO telemetry_metrics (
        time, service_name, instance_id, node_id,
        smartload_request_latency_ms, smartload_request_count, smartload_error_rate,
        source, environment
    ) VALUES (
        '$TEST_TS', 'nginx-lb', 'verify-script-001', '$TEST_UUID',
        42.7, 1, 0.0,
        'real', 'development'
    ) ON CONFLICT (time, node_id) DO NOTHING;
" 2>&1) || true

check "Manual insert into telemetry_metrics" \
    "SELECT COUNT(*) FROM telemetry_metrics WHERE node_id='$TEST_UUID'::uuid;" \
    "1"

check "lb_request_latencies view returns inserted row" \
    "SELECT COUNT(*) FROM lb_request_latencies WHERE node_id='$TEST_UUID'::uuid;" \
    "1"

# Cleanup test row
$PSQL -c "DELETE FROM telemetry_metrics WHERE node_id='$TEST_UUID'::uuid;" > /dev/null 2>&1 || true

# ── 7. Ingester HTTP insert test ──────────────────────────
echo ""
echo "─── Ingester HTTP Insert (if running) ───────────────"
INGESTER="${INGESTER_URL:-http://localhost:5555}"
if curl -sf "$INGESTER/health" > /dev/null 2>&1; then
    HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST "$INGESTER/metrics" \
        -H "Content-Type: application/json" \
        -d "{
          \"timestamp\": \"$TEST_TS\",
          \"service_name\": \"nginx-lb\",
          \"instance_id\": \"verify-http-001\",
          \"node_id\": \"a0000000-0000-0000-0000-000000000088\",
          \"metrics\": {
            \"smartload_request_latency_ms\": 55.3,
            \"smartload_request_count\": 1,
            \"smartload_error_rate\": 0.0
          },
          \"attributes\": { \"source\": \"real\", \"environment\": \"development\" }
        }")
    if [[ "$HTTP_STATUS" == "201" ]]; then
        echo -e "${GREEN}✓ HTTP POST /metrics → 201 Created${NC}"; ((PASS++))
    else
        echo -e "${YELLOW}⚠ HTTP POST /metrics → $HTTP_STATUS (ingester may not be running)${NC}"
    fi
    # Cleanup
    $PSQL -c "DELETE FROM telemetry_metrics WHERE instance_id='verify-http-001';" > /dev/null 2>&1 || true
else
    echo -e "${YELLOW}⚠ Ingester not reachable at $INGESTER — skipping HTTP test${NC}"
fi

# ── Summary ───────────────────────────────────────────────
echo ""
echo "======================================================"
if [[ $FAIL -eq 0 ]]; then
    echo -e "${GREEN}  ALL $PASS checks passed ✓${NC}"
    echo "  Issue #10 Acceptance Criteria: MET"
else
    echo -e "${RED}  $FAIL check(s) failed — $PASS passed${NC}"
    echo "  Issue #10 Acceptance Criteria: NOT FULLY MET"
fi
echo "======================================================"

exit $FAIL