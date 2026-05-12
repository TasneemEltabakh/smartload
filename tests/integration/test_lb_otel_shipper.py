"""
tests/integration/test_lb_otel_shipper.py
─────────────────────────────────────────
T1.2 acceptance per SOT §8.1.1:
  - Pure-Python: line_to_datapoints / build_envelope produce the canonical
    flat metric names with valid OTLP/HTTP-JSON shape (runs in unit-tests
    CI without the stack).
  - Live-stack: driving traffic through the load balancer results in
    per-request rows appearing in the `metrics` table with
    STDDEV(request_latency_ms) > 0 — the per-request-fidelity proof that
    motivated picking Approach B over the F5 nginx-otel module.
"""

from __future__ import annotations

import os
import sys
import time
import uuid

import pytest

# ── make the shipper module importable without docker ─────────────────────────
_SHIPPER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "lb-otel-shipper",
)
if _SHIPPER_DIR not in sys.path:
    sys.path.insert(0, _SHIPPER_DIR)

import app as shipper  # noqa: E402  — pure-Python helpers only


# ── pure-Python: parser + envelope shape ──────────────────────────────────────

class TestLineToDataPoints:

    def test_valid_line_emits_three_metrics(self):
        line = (
            '{"timestamp":"2026-05-12T10:00:00+00:00","service":"nginx",'
            '"client_ip":"10.0.0.1","request":"GET / HTTP/1.1","request_path":"/",'
            '"status":200,"backend":"test-backend:8080","latency":0.0123,'
            '"upstream_latency":"0.011"}'
        )
        dps = shipper.line_to_datapoints(line, now_ns=1_700_000_000_000_000_000)

        names = [name for name, _, _ in dps]
        assert names == ["request_count", "request_latency_ms", "error_rate"]

        values = {name: value for name, value, _ in dps}
        assert values["request_count"]      == 1.0
        assert values["request_latency_ms"] == pytest.approx(12.3, rel=1e-6)
        assert values["error_rate"]         == 0.0

    def test_5xx_status_flags_error(self):
        line = '{"status":503,"latency":0.001}'
        dps  = shipper.line_to_datapoints(line, now_ns=1)
        assert dict((n, v) for n, v, _ in dps)["error_rate"] == 1.0

    def test_4xx_status_is_not_an_error(self):
        line = '{"status":404,"latency":0.001}'
        dps  = shipper.line_to_datapoints(line, now_ns=1)
        assert dict((n, v) for n, v, _ in dps)["error_rate"] == 0.0

    @pytest.mark.parametrize("bad", [
        "not-json",                  # malformed
        "{}",                        # empty
        '{"status":200}',            # missing latency
        '{"latency":0.01}',          # missing status
        '{"status":"x","latency":1}',# unparseable status
        '{"status":200,"latency":"x"}',
    ])
    def test_malformed_lines_yield_no_datapoints(self, bad):
        assert shipper.line_to_datapoints(bad, now_ns=1) == []


class TestBuildEnvelope:

    def test_envelope_groups_datapoints_by_metric_name(self):
        dps = [
            ("request_count",      1.0,  1),
            ("request_latency_ms", 12.3, 1),
            ("error_rate",         0.0,  1),
            ("request_count",      1.0,  2),
            ("request_latency_ms", 8.7,  2),
            ("error_rate",         0.0,  2),
        ]
        env = shipper.build_envelope(dps)

        metrics = env["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        by_name = {m["name"]: m for m in metrics}
        assert set(by_name) == {"request_count", "request_latency_ms", "error_rate"}
        for m in metrics:
            assert len(m["gauge"]["dataPoints"]) == 2
            for dp in m["gauge"]["dataPoints"]:
                assert "timeUnixNano" in dp and "asDouble" in dp

    def test_resource_attributes_match_sot_contract(self):
        env  = shipper.build_envelope([("request_count", 1.0, 1)])
        attrs = {
            a["key"]: a["value"]["stringValue"]
            for a in env["resourceMetrics"][0]["resource"]["attributes"]
        }
        assert attrs["service.name"] == "load-balancer"
        assert attrs.get("service.instance.id")  # non-empty hostname


# ── live-stack: per-request fidelity proof ────────────────────────────────────

def _can_reach() -> bool:
    try:
        import psycopg2
        import requests
        from tests.integration.conftest import SERVICE_URLS, TIMESCALEDB_DSN
        requests.get(SERVICE_URLS["load-balancer"], timeout=2)
        psycopg2.connect(TIMESCALEDB_DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _can_reach(), reason="stack not running")
class TestPerRequestFidelity:
    """SOT §8.1.1 review checklist: the integration test must assert
    STDDEV > 0, not just row count. STDDEV > 0 over distinct requests is
    the property that fails if a future change accidentally pre-aggregates
    requests (e.g. switching to nginx-otel module + spanmetrics)."""

    def test_traffic_lands_per_request_with_nonzero_stddev(self):
        import psycopg2
        import requests
        from tests.integration.conftest import SERVICE_URLS, TIMESCALEDB_DSN

        marker_path = f"/__shipper_probe_{uuid.uuid4().hex[:8]}"
        # Drive ≥ 30 requests through the LB. NGINX latency naturally
        # varies even on localhost; we don't need to synthesize it.
        sent = 0
        for _ in range(35):
            try:
                requests.get(SERVICE_URLS["load-balancer"] + marker_path, timeout=2)
                sent += 1
            except requests.RequestException:
                pass
        assert sent >= 30, f"could only drive {sent} requests through LB"

        # Allow up to ~12 s for: NGINX flush, shipper tail loop pickup,
        # collector batch (5 s), telemetry DB insert.
        deadline = time.time() + 15.0
        rows = 0
        stddev = None
        while time.time() < deadline:
            with psycopg2.connect(TIMESCALEDB_DSN) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*), STDDEV(value)
                    FROM metrics
                    WHERE service = 'load-balancer'
                      AND metric_name = 'request_latency_ms'
                      AND time > NOW() - INTERVAL '60 seconds'
                    """
                )
                rows, stddev = cur.fetchone()
            if rows and rows >= 30:
                break
            time.sleep(1.0)

        assert rows >= 30, f"only {rows} request_latency_ms rows landed"
        assert stddev is not None and stddev > 0, (
            f"STDDEV(request_latency_ms) = {stddev!r} — per-request fidelity "
            "lost (the shipper or an upstream stage is pre-aggregating)"
        )

    def test_request_count_and_error_rate_also_persisted(self):
        import psycopg2
        from tests.integration.conftest import TIMESCALEDB_DSN

        with psycopg2.connect(TIMESCALEDB_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT metric_name, COUNT(*)
                FROM metrics
                WHERE service = 'load-balancer'
                  AND time > NOW() - INTERVAL '60 seconds'
                GROUP BY metric_name
                """
            )
            counts = dict(cur.fetchall())
        # All three canonical flat metric names must be flowing — the
        # anomaly detector's ANOMALY_QUERY filters on these names.
        assert counts.get("request_count", 0)      >= 30
        assert counts.get("request_latency_ms", 0) >= 30
        assert counts.get("error_rate", 0)         >= 30
