"""
tests/unit/shared/test_metrics.py
──────────────────────────────────
Unit tests for the shared Prometheus instrumentation (#161,
services/shared/metrics.py).

prometheus_client uses a process-global default registry, so each test uses a
distinct metric prefix to avoid "Duplicated timeseries" collisions — exactly
how separate service processes each own their prefixed metrics.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make services/shared importable (the same path trick the services use).
_SERVICES = Path(__file__).resolve().parents[3] / "services"
if str(_SERVICES) not in sys.path:
    sys.path.insert(0, str(_SERVICES))

from prometheus_client import REGISTRY  # noqa: E402

from shared.metrics import ServiceMetrics, metrics_response  # noqa: E402


def _val(name: str, **labels):
    return REGISTRY.get_sample_value(name, labels or None)


def test_up_is_set_on_construction():
    ServiceMetrics("t_up")
    assert _val("t_up_up") == 1.0


def test_time_cycle_counts_and_times():
    m = ServiceMetrics("t_cycle")
    with m.time_cycle() as c:
        c["outcome"] = "published"
    assert _val("t_cycle_cycle_total", outcome="published") == 1.0
    assert _val("t_cycle_cycle_duration_seconds_count") == 1.0


def test_time_cycle_default_outcome_is_ok():
    m = ServiceMetrics("t_cycle_ok")
    with m.time_cycle():
        pass
    assert _val("t_cycle_ok_cycle_total", outcome="ok") == 1.0


def test_time_cycle_records_error_and_reraises():
    m = ServiceMetrics("t_cycle_err")
    with pytest.raises(ValueError):
        with m.time_cycle():
            raise ValueError("boom")
    assert _val("t_cycle_err_cycle_total", outcome="error") == 1.0


def test_time_publish_counts_channel_outcome():
    m = ServiceMetrics("t_pub")
    with m.time_publish("smartload.x"):
        pass
    assert _val("t_pub_publish_total", channel="smartload.x", outcome="ok") == 1.0
    assert _val("t_pub_publish_duration_seconds_count") == 1.0


def test_time_publish_records_error_and_reraises():
    m = ServiceMetrics("t_pub_err")
    with pytest.raises(RuntimeError):
        with m.time_publish("smartload.y"):
            raise RuntimeError("nope")
    assert _val("t_pub_err_publish_total", channel="smartload.y", outcome="error") == 1.0


def test_record_publish_without_timing():
    m = ServiceMetrics("t_rec")
    m.record_publish("smartload.z")
    m.record_publish("smartload.z", outcome="error")
    assert _val("t_rec_publish_total", channel="smartload.z", outcome="ok") == 1.0
    assert _val("t_rec_publish_total", channel="smartload.z", outcome="error") == 1.0


def test_metrics_response_is_prometheus_text_004():
    body, content_type = metrics_response()
    assert isinstance(body, bytes)
    assert b"_up" in body
    assert "text/plain" in content_type and "0.0.4" in content_type
