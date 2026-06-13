"""
tests/integration/test_resource_collector.py
─────────────────────────────────────────────
Acceptance for the host-resource shipper (SOT §8.1.2):
  - Pure-Python: the Docker-stats maths (CPU delta normalised to online
    cores; memory net of reclaimable cache) and the OTLP envelope shape
    are correct without a Docker daemon or the live stack.
  - Live-stack: the collector's gauges land in the `metrics` table and the
    telemetry /api/v1/metrics/resources endpoint pivots them into one
    record per instance.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

# ── make the collector module importable (it's a flat app.py, not a package) ──
_COLLECTOR_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "resource-collector",
)
if _COLLECTOR_DIR not in sys.path:
    sys.path.insert(0, _COLLECTOR_DIR)

import app as collector  # noqa: E402  — pure-Python helpers only (no docker.from_env at import)


def _stats(*, cpu_total, precpu_total, system_cur, system_pre,
           online=1, usage=None, limit=None, reclaim_key="inactive_file",
           reclaim=0):
    """Build a minimal Docker stats sample shaped like the Engine API."""
    sample = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": cpu_total},
            "system_cpu_usage": system_cur,
            "online_cpus": online,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": precpu_total},
            "system_cpu_usage": system_pre,
        },
    }
    if usage is not None:
        sample["memory_stats"] = {
            "usage": usage,
            "limit": limit,
            "stats": {reclaim_key: reclaim},
        }
    return sample


# ── pure-Python: CPU maths ────────────────────────────────────────────────────

class TestComputeCpuPercent:

    def test_single_core_delta(self):
        # 10% of one core: cpu delta 100 over system delta 1000.
        s = _stats(cpu_total=200, precpu_total=100,
                   system_cur=2000, system_pre=1000, online=1)
        assert collector.compute_cpu_percent(s) == pytest.approx(10.0)

    def test_scaled_by_online_cores(self):
        # Same per-core ratio, 4 cores → 40%.
        s = _stats(cpu_total=200, precpu_total=100,
                   system_cur=2000, system_pre=1000, online=4)
        assert collector.compute_cpu_percent(s) == pytest.approx(40.0)

    def test_online_falls_back_to_percpu_length(self):
        s = _stats(cpu_total=200, precpu_total=100,
                   system_cur=2000, system_pre=1000, online=None)
        s["cpu_stats"]["cpu_usage"]["percpu_usage"] = [0, 0]   # 2 cores
        assert collector.compute_cpu_percent(s) == pytest.approx(20.0)

    def test_zero_baseline_first_read_returns_none(self):
        # Docker's first one-shot read has a zeroed precpu baseline.
        s = _stats(cpu_total=5_000, precpu_total=0,
                   system_cur=10_000, system_pre=0, online=4)
        assert collector.compute_cpu_percent(s) is None

    def test_zero_system_delta_returns_none(self):
        s = _stats(cpu_total=200, precpu_total=100,
                   system_cur=2000, system_pre=2000, online=4)
        assert collector.compute_cpu_percent(s) is None

    def test_malformed_sample_returns_none(self):
        assert collector.compute_cpu_percent({}) is None
        assert collector.compute_cpu_percent({"cpu_stats": {}}) is None


# ── pure-Python: memory maths ─────────────────────────────────────────────────

class TestComputeMemory:

    def test_cgroup_v2_subtracts_inactive_file(self):
        used, limit, pct = collector.compute_memory(
            _stats(cpu_total=1, precpu_total=1, system_cur=1, system_pre=1,
                   usage=200, limit=1000, reclaim_key="inactive_file", reclaim=50)
        )
        assert used == 150.0
        assert limit == 1000.0
        assert pct == pytest.approx(15.0)

    def test_cgroup_v1_cache_key(self):
        used, _, _ = collector.compute_memory(
            _stats(cpu_total=1, precpu_total=1, system_cur=1, system_pre=1,
                   usage=300, limit=1000, reclaim_key="cache", reclaim=100)
        )
        assert used == 200.0

    def test_missing_memory_stats_returns_none_triple(self):
        s = _stats(cpu_total=1, precpu_total=1, system_cur=1, system_pre=1)
        assert collector.compute_memory(s) == (None, None, None)

    def test_zero_limit_yields_no_percent(self):
        used, limit, pct = collector.compute_memory(
            _stats(cpu_total=1, precpu_total=1, system_cur=1, system_pre=1,
                   usage=200, limit=0)
        )
        assert used == 200.0
        assert limit is None
        assert pct is None


# ── pure-Python: datapoint assembly + instance keying ─────────────────────────

class TestDatapointsFor:

    def test_emits_four_metrics_when_all_present(self):
        s = _stats(cpu_total=200, precpu_total=100, system_cur=2000,
                   system_pre=1000, online=1, usage=200, limit=1000, reclaim=50)
        names = [n for n, *_ in collector.datapoints_for(s, "rl-engine", "smartload-rl-engine-1", 42)]
        assert names == ["cpu_percent", "memory_used_bytes",
                         "memory_limit_bytes", "memory_percent"]

    def test_skips_cpu_on_first_read_but_keeps_memory(self):
        s = _stats(cpu_total=5000, precpu_total=0, system_cur=10000,
                   system_pre=0, usage=200, limit=1000)
        names = [n for n, *_ in collector.datapoints_for(s, "svc", "inst", 1)]
        assert "cpu_percent" not in names
        assert "memory_used_bytes" in names

    def test_tuples_carry_service_and_instance(self):
        s = _stats(cpu_total=200, precpu_total=100, system_cur=2000,
                   system_pre=1000, online=1)
        dps = collector.datapoints_for(s, "forecasting", "host-x", 99)
        for _name, _value, ts, service, instance in dps:
            assert ts == 99 and service == "forecasting" and instance == "host-x"


class TestInstanceFor:

    def test_backend_gets_port_suffix(self):
        # Must match the lb-otel-shipper's canonical "<name>:8080" so the UI
        # can join CPU with rps/latency per backend.
        assert collector.instance_for("test-backend", "smartload-test-backend-3") \
            == "smartload-test-backend-3:8080"

    def test_non_backend_is_bare_name(self):
        assert collector.instance_for("anomaly-detector", "smartload-anomaly-detector-1") \
            == "smartload-anomaly-detector-1"


# ── pure-Python: OTLP envelope shape ──────────────────────────────────────────

class TestBuildEnvelope:

    def test_groups_one_resource_block_per_service(self):
        env = collector.build_envelope([
            ("cpu_percent", 10.0, 1, "rl-engine", "rl-1"),
            ("cpu_percent", 20.0, 1, "forecasting", "fc-1"),
        ])
        services = {
            a["value"]["stringValue"]
            for rm in env["resourceMetrics"]
            for a in rm["resource"]["attributes"]
            if a["key"] == "service.name"
        }
        assert services == {"rl-engine", "forecasting"}

    def test_datapoint_carries_instance_attribute(self):
        env = collector.build_envelope([
            ("memory_used_bytes", 1024.0, 7, "test-backend", "smartload-test-backend-1:8080"),
        ])
        rm = env["resourceMetrics"][0]
        dp = rm["scopeMetrics"][0]["metrics"][0]["gauge"]["dataPoints"][0]
        inst = next(a["value"]["stringValue"] for a in dp["attributes"]
                    if a["key"] == "instance")
        assert inst == "smartload-test-backend-1:8080"
        assert dp["asDouble"] == 1024.0 and "timeUnixNano" in dp

    def test_empty_input_yields_no_resource_metrics(self):
        assert collector.build_envelope([]) == {"resourceMetrics": []}


# ── live-stack: collector → metrics table → telemetry endpoint ────────────────

def _reach():
    try:
        import psycopg2
        import requests
        from tests.integration.conftest import SERVICE_URLS, TIMESCALEDB_DSN
        requests.get(SERVICE_URLS["telemetry"] + "/health", timeout=2)
        psycopg2.connect(TIMESCALEDB_DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _reach(), reason="stack not running")
class TestResourceMetricsFlow:

    def test_cpu_rows_land_in_metrics(self):
        import psycopg2
        from tests.integration.conftest import TIMESCALEDB_DSN

        # Collector ships every ~15s; give two cycles + DB insert headroom.
        deadline = time.time() + 40.0
        rows = 0
        while time.time() < deadline:
            with psycopg2.connect(TIMESCALEDB_DSN) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM metrics
                    WHERE metric_name = 'cpu_percent'
                      AND time > NOW() - INTERVAL '120 seconds'
                    """
                )
                rows = cur.fetchone()[0]
            if rows > 0:
                break
            time.sleep(3.0)
        assert rows > 0, "no cpu_percent rows landed — is resource-collector up?"

    def test_telemetry_resources_endpoint_pivots_per_instance(self):
        import requests
        from tests.integration.conftest import SERVICE_URLS

        r = requests.get(
            SERVICE_URLS["telemetry"] + "/api/v1/metrics/resources",
            params={"window": 120}, timeout=5,
        )
        assert r.status_code == 200
        body = r.json()
        assert "instances" in body
        # At least one instance should report a CPU figure once the stack
        # has been up for ≥2 collector cycles.
        with_cpu = [i for i in body["instances"] if i.get("cpu_percent") is not None]
        assert with_cpu, f"no instance reported cpu_percent: {body}"
        sample = with_cpu[0]
        assert set(sample) >= {
            "instance", "service", "cpu_percent",
            "memory_used_bytes", "memory_limit_bytes", "memory_percent", "time",
        }
