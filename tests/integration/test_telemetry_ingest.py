"""
tests/integration/test_telemetry_ingest.py
───────────────────────────────────────────
T1.1 acceptance: telemetry service receives OTLP/HTTP-JSON, persists rows
to the canonical `metrics` hypertable, and serves them back via the read
API. Per SOT §8.3 acceptance criteria for T1.1:
  - Rows visible in metrics: SELECT * FROM metrics ORDER BY time DESC LIMIT 20
  - GET /api/v1/metrics?service=load-balancer&window=5m returns JSON

Skips with a clear reason if the docker stack is not running, matching the
pattern used by test_s2_baseline.py.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import psycopg2
import pytest
import requests

from tests.integration.conftest import SERVICE_URLS, TIMESCALEDB_DSN


def _can_reach() -> bool:
    try:
        requests.get(SERVICE_URLS["telemetry"] + "/health", timeout=2)
        psycopg2.connect(TIMESCALEDB_DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _can_reach(),
    reason="telemetry + TimescaleDB not reachable; run `docker compose up -d` first",
)


def _envelope(service: str, instance: str, metric: str, value: float, ts_ns: int) -> dict:
    return {
        "resourceMetrics": [{
            "resource": {"attributes": [
                {"key": "service.name",        "value": {"stringValue": service}},
                {"key": "service.instance.id", "value": {"stringValue": instance}},
            ]},
            "scopeMetrics": [{"metrics": [{
                "name":  metric,
                "gauge": {"dataPoints": [{
                    "timeUnixNano": str(ts_ns),
                    "asDouble":     value,
                }]},
            }]}],
        }],
    }


def _now_ns() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)


class TestOTLPIngest:

    def test_otlp_post_persists_to_metrics(self):
        # Unique synthetic service name so this test is isolated from
        # whatever else is hitting the stack.
        service  = f"itest-{uuid.uuid4().hex[:8]}"
        instance = "backend_t1"
        metric   = "request_latency_ms"
        body     = _envelope(service, instance, metric, 42.0, _now_ns())

        resp = requests.post(SERVICE_URLS["telemetry"] + "/v1/metrics", json=body, timeout=10)
        assert resp.status_code == 200, resp.text
        assert resp.json()["accepted"] == 1

        with psycopg2.connect(TIMESCALEDB_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT instance, metric_name, value FROM metrics WHERE service = %s",
                (service,),
            )
            rows = cur.fetchall()
        assert rows == [(instance, metric, 42.0)]

    def test_read_api_returns_recent_rows(self):
        service  = f"itest-{uuid.uuid4().hex[:8]}"
        instance = "backend_t1"
        body     = _envelope(service, instance, "request_count", 17.0, _now_ns())

        post = requests.post(SERVICE_URLS["telemetry"] + "/v1/metrics", json=body, timeout=10)
        assert post.status_code == 200

        resp = requests.get(
            SERVICE_URLS["telemetry"] + "/api/v1/metrics",
            params={"service": service, "window": "5m"},
            timeout=10,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == service
        assert data["window"]  == "5m"
        assert len(data["rows"]) == 1
        row = data["rows"][0]
        assert row["instance"]    == instance
        assert row["metric_name"] == "request_count"
        assert row["value"]       == 17.0

    @pytest.mark.parametrize("window", ["junk", "0s", "-5m", "5", ""])
    def test_read_api_validates_window(self, window):
        resp = requests.get(
            SERVICE_URLS["telemetry"] + "/api/v1/metrics",
            params={"service": "x", "window": window},
            timeout=5,
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "ValidationError"

    def test_read_api_requires_service(self):
        resp = requests.get(
            SERVICE_URLS["telemetry"] + "/api/v1/metrics",
            params={"window": "5m"},
            timeout=5,
        )
        assert resp.status_code == 400

    def test_otlp_rejects_non_json(self):
        resp = requests.post(
            SERVICE_URLS["telemetry"] + "/v1/metrics",
            data=b"\x00\x01",
            headers={"Content-Type": "application/x-protobuf"},
            timeout=5,
        )
        assert resp.status_code == 415

    def test_otlp_drops_unsupported_metric_type(self):
        # Histograms / summaries aren't in the SmartLoad metric set per
        # SOT §8.3 — accepted but counted as dropped-shape, not persisted.
        before = requests.get(SERVICE_URLS["telemetry"] + "/api/v1/stats", timeout=5).json()

        body = {"resourceMetrics": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "load-balancer"}},
            ]},
            "scopeMetrics": [{"metrics": [{
                "name":      "latency_histogram",
                "histogram": {"dataPoints": [{
                    "timeUnixNano": str(int(time.time() * 1e9)),
                    "count":        "10",
                    "sum":          100.0,
                }]},
            }]}],
        }]}
        resp = requests.post(SERVICE_URLS["telemetry"] + "/v1/metrics", json=body, timeout=5)
        assert resp.status_code == 200
        assert resp.json().get("accepted", 0) == 0

        after = requests.get(SERVICE_URLS["telemetry"] + "/api/v1/stats", timeout=5).json()
        assert after["rows_dropped_shape"] >= before["rows_dropped_shape"] + 1

    def test_stats_counters_advance_on_successful_write(self):
        before = requests.get(SERVICE_URLS["telemetry"] + "/api/v1/stats", timeout=5).json()

        service = f"itest-{uuid.uuid4().hex[:8]}"
        body    = _envelope(service, "backend_t1", "request_latency_ms", 1.0, _now_ns())
        requests.post(SERVICE_URLS["telemetry"] + "/v1/metrics", json=body, timeout=5)

        after = requests.get(SERVICE_URLS["telemetry"] + "/api/v1/stats", timeout=5).json()
        assert after["rows_written"]    >= before["rows_written"]    + 1
        assert after["batches_written"] >= before["batches_written"] + 1

    def test_otlp_uses_datapoint_instance_when_resource_omits_it(self):
        # If service.instance.id is missing on the resource, fall through to
        # the datapoint-level `instance` attribute, then to "unknown".
        service  = f"itest-{uuid.uuid4().hex[:8]}"
        body = {
            "resourceMetrics": [{
                "resource": {"attributes": [
                    {"key": "service.name", "value": {"stringValue": service}},
                ]},
                "scopeMetrics": [{"metrics": [{
                    "name": "request_latency_ms",
                    "gauge": {"dataPoints": [{
                        "timeUnixNano": str(_now_ns()),
                        "asDouble":     7.0,
                        "attributes": [
                            {"key": "instance", "value": {"stringValue": "backend_dp"}},
                        ],
                    }]},
                }]}],
            }],
        }
        resp = requests.post(SERVICE_URLS["telemetry"] + "/v1/metrics", json=body, timeout=5)
        assert resp.status_code == 200

        with psycopg2.connect(TIMESCALEDB_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT instance FROM metrics WHERE service = %s",
                (service,),
            )
            assert cur.fetchall() == [("backend_dp",)]
