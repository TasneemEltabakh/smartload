"""
tests/integration/test_telemetry_parser.py
───────────────────────────────────────────
Pure-Python tests for services/telemetry/app.py:parse_otlp_to_rows and
the window-string helper. No docker stack required — runs in the
unit-tests CI job alongside test_s2_baseline.py.

The OTLP/HTTP-JSON shapes covered here are the subset the SmartLoad
telemetry receiver actually accepts: gauge + sum + asDouble/asInt,
service.name + service.instance.id resource attributes, datapoint-level
`instance` attribute fall-through. Histograms / summaries are treated
as out-of-scope drops.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys

import pytest

# Load services/telemetry/app.py without requiring it to be a package, so
# this test runs from a clean checkout. The app.py itself adds the
# canonical `services/shared/` to sys.path on import.
_REPO = pathlib.Path(__file__).resolve().parents[2]
_APP  = _REPO / "services" / "telemetry" / "app.py"

_spec = importlib.util.spec_from_file_location("telemetry_app", _APP)
telemetry_app = importlib.util.module_from_spec(_spec)
sys.modules["telemetry_app"] = telemetry_app
_spec.loader.exec_module(telemetry_app)


# ── parse_otlp_to_rows ───────────────────────────────────────────────────────

def _gauge(name: str, value: float, ts_ns: int = 1_715_200_000_000_000_000):
    return {
        "name":  name,
        "gauge": {"dataPoints": [{"timeUnixNano": str(ts_ns), "asDouble": value}]},
    }


def _resource(service: str | None = "load-balancer", instance: str | None = "backend_1"):
    attrs = []
    if service is not None:
        attrs.append({"key": "service.name", "value": {"stringValue": service}})
    if instance is not None:
        attrs.append({"key": "service.instance.id", "value": {"stringValue": instance}})
    return {"attributes": attrs}


def _envelope(metrics: list[dict], resource: dict | None = None) -> dict:
    return {
        "resourceMetrics": [{
            "resource":     resource if resource is not None else _resource(),
            "scopeMetrics": [{"metrics": metrics}],
        }],
    }


class TestParseOTLP:

    def test_canonical_gauge_extracts_one_row(self):
        rows = telemetry_app.parse_otlp_to_rows(
            _envelope([_gauge("request_latency_ms", 42.5)])
        )
        assert len(rows) == 1
        ts, service, instance, name, value = rows[0]
        assert service  == "load-balancer"
        assert instance == "backend_1"
        assert name     == "request_latency_ms"
        assert value    == 42.5
        assert ts.tzinfo is not None

    def test_sum_metric_also_extracted(self):
        body = _envelope([{
            "name": "request_count",
            "sum":  {"dataPoints": [{"timeUnixNano": "1715200000000000000", "asInt": "17"}]},
        }])
        rows = telemetry_app.parse_otlp_to_rows(body)
        assert len(rows) == 1
        assert rows[0][3] == "request_count"
        assert rows[0][4] == 17.0

    def test_histogram_dropped_with_counter(self):
        before = telemetry_app._rows_dropped_shape
        body = _envelope([{
            "name":      "latency_histogram",
            "histogram": {"dataPoints": [{"timeUnixNano": "1715200000000000000", "count": "1", "sum": 1.0}]},
        }])
        rows = telemetry_app.parse_otlp_to_rows(body)
        assert rows == []
        assert telemetry_app._rows_dropped_shape == before + 1

    def test_missing_resource_instance_falls_through_to_datapoint_attr(self):
        body = _envelope(
            [{
                "name":  "request_latency_ms",
                "gauge": {"dataPoints": [{
                    "timeUnixNano": "1715200000000000000",
                    "asDouble":     7.0,
                    "attributes": [
                        {"key": "instance", "value": {"stringValue": "backend_dp"}},
                    ],
                }]},
            }],
            resource=_resource(instance=None),
        )
        rows = telemetry_app.parse_otlp_to_rows(body)
        assert rows[0][2] == "backend_dp"

    def test_missing_service_name_becomes_unknown(self):
        body = _envelope(
            [_gauge("request_latency_ms", 1.0)],
            resource=_resource(service=None),
        )
        rows = telemetry_app.parse_otlp_to_rows(body)
        assert rows[0][1] == "unknown"

    def test_datapoint_without_value_is_dropped(self):
        before = telemetry_app._rows_dropped_shape
        body = _envelope([{
            "name":  "request_latency_ms",
            "gauge": {"dataPoints": [{"timeUnixNano": "1715200000000000000"}]},  # no asDouble/asInt
        }])
        assert telemetry_app.parse_otlp_to_rows(body) == []
        assert telemetry_app._rows_dropped_shape == before + 1

    def test_empty_envelope_yields_no_rows(self):
        assert telemetry_app.parse_otlp_to_rows({}) == []
        assert telemetry_app.parse_otlp_to_rows({"resourceMetrics": []}) == []

    def test_metric_without_name_is_skipped_silently(self):
        # A metric entry missing `name` is malformed but shouldn't crash;
        # it just doesn't contribute rows.
        body = _envelope([{"gauge": {"dataPoints": [{"timeUnixNano": "1", "asDouble": 1.0}]}}])
        assert telemetry_app.parse_otlp_to_rows(body) == []


# ── _window_to_interval ──────────────────────────────────────────────────────

class TestWindowParser:

    @pytest.mark.parametrize("text, expected", [
        ("30s",  "30 seconds"),
        ("5m",   "5 minutes"),
        ("1h",   "1 hours"),
        ("2d",   "2 days"),
        (" 5m ", "5 minutes"),
    ])
    def test_valid(self, text, expected):
        assert telemetry_app._window_to_interval(text) == expected

    @pytest.mark.parametrize("text", ["", "junk", "0s", "-5m", "5", "5x", "m5", None])
    def test_invalid_returns_none(self, text):
        assert telemetry_app._window_to_interval(text or "") is None
