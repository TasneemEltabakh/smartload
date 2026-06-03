"""
tests/unit/operator-ui/test_aggregator.py
──────────────────────────────────────────
Pure-Python unit tests for services/operator-ui/bff/aggregator.py.

No Flask, no Redis, no network. The aggregator's whole point is that every
IO is injected — these tests pin the composition with stub callables.

Coverage:
  1. fetch_service_status — happy path, timeout, connection error,
                            non-200, non-dict body, malformed JSON,
                            forward-compat extra fields pass through.
  2. compute_overall      — every cell of the truth table.
  3. build_status_response — fan-out shape, ordering, best-effort policy
                              and audit fetches, empty service_urls,
                              service down doesn't crash the overall response.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BFF = Path(__file__).resolve().parents[2].parent / "services" / "operator-ui" / "bff"
if str(_BFF) not in sys.path:
    sys.path.insert(0, str(_BFF))

from aggregator import (  # noqa: E402
    DEFAULT_TIMEOUT_S,
    build_status_response,
    compute_overall,
    fetch_service_status,
)


class _FakeResponse:
    """Stand-in for `httpx.Response`."""

    def __init__(self, status_code=200, json_body=None, raise_on_json=False):
        self.status_code = status_code
        self._json_body = json_body
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json:
            raise ValueError("not json")
        return self._json_body


def _stub_get(url_to_response: dict, sleep_per_url: dict | None = None):
    """Build an http_get stub keyed by URL substring."""

    def _get(url, timeout=None):
        if sleep_per_url:
            import time
            for needle, secs in sleep_per_url.items():
                if needle in url:
                    time.sleep(secs)
        for needle, resp in url_to_response.items():
            if needle in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise ConnectionError(f"no stub for {url}")
    return _get


# ── per-service fetch ────────────────────────────────────────────────────────

class TestFetchServiceStatus:
    def test_happy_path_returns_canonical_shape(self):
        http_get = _stub_get({
            "policy-manager": _FakeResponse(200, {
                "status": "ok",
                "redis": True,
                "timescaledb": True,
                "policy_version": 29,
                "service": "policy-manager",  # dropped (redundant)
            }),
        })
        name, status = fetch_service_status("policy-manager", "http://policy-manager:8086", http_get)
        assert name == "policy-manager"
        assert status["status"] == "ok"
        assert status["redis"] is True
        assert status["timescaledb"] is True
        assert status["policy_version"] == 29
        assert "service" not in status  # the `service` key is dropped

    def test_forward_compat_extra_fields_pass_through(self):
        # A service that adds a new /health field in the future shouldn't
        # be silently dropped by the aggregator.
        http_get = _stub_get({
            "rl-engine": _FakeResponse(200, {
                "status": "ok",
                "runloop_enabled": True,
                "policy": "ppo",
                "mode": "active",
                "new_future_field": "value",  # not in _KNOWN_HEALTH_EXTRAS
            }),
        })
        _, status = fetch_service_status("rl-engine", "http://rl-engine:8084", http_get)
        assert status["new_future_field"] == "value"

    def test_connection_error_becomes_status_down(self):
        http_get = _stub_get({"telemetry": ConnectionRefusedError("boom")})
        _, status = fetch_service_status("telemetry", "http://telemetry:8081", http_get)
        assert status["status"] == "down"
        assert status["error"] == "ConnectionRefusedError"

    def test_timeout_becomes_status_down(self):
        class _TimeoutError(Exception):
            pass
        http_get = _stub_get({"forecasting": _TimeoutError("slow")})
        _, status = fetch_service_status("forecasting", "http://forecasting:8083", http_get)
        assert status["status"] == "down"
        assert status["error"] == "_TimeoutError"

    def test_non_200_becomes_down(self):
        http_get = _stub_get({"autoscaler": _FakeResponse(503, {"status": "ok"})})
        _, status = fetch_service_status("autoscaler", "http://autoscaler:8085", http_get)
        assert status["status"] == "down"
        assert status["error"] == "http_503"

    def test_non_dict_json_body_treated_as_down(self):
        http_get = _stub_get({"x": _FakeResponse(200, ["array", "not", "object"])})
        _, status = fetch_service_status("x", "http://x", http_get)
        assert status["status"] == "down"

    def test_malformed_json_body_treated_as_down(self):
        http_get = _stub_get({"x": _FakeResponse(200, raise_on_json=True)})
        _, status = fetch_service_status("x", "http://x", http_get)
        assert status["status"] == "down"

    def test_trailing_slash_normalised(self):
        captured = {}

        def _get(url, timeout=None):
            captured["url"] = url
            return _FakeResponse(200, {"status": "ok"})

        fetch_service_status("policy-manager", "http://policy-manager:8086/", _get)
        assert captured["url"] == "http://policy-manager:8086/health"


# ── overall composition ──────────────────────────────────────────────────────

class TestComputeOverall:
    def test_all_ok(self):
        services = {"a": {"status": "ok"}, "b": {"status": "ok"}}
        assert compute_overall(services) == "ok"

    def test_any_degraded_is_degraded(self):
        services = {"a": {"status": "ok"}, "b": {"status": "degraded"}}
        assert compute_overall(services) == "degraded"

    def test_any_down_is_down(self):
        services = {"a": {"status": "ok"}, "b": {"status": "down"}}
        assert compute_overall(services) == "down"

    def test_down_dominates_degraded(self):
        services = {
            "a": {"status": "degraded"},
            "b": {"status": "down"},
            "c": {"status": "ok"},
        }
        assert compute_overall(services) == "down"

    def test_unknown_status_treated_as_degraded(self):
        services = {"a": {"status": "weird"}}
        assert compute_overall(services) == "degraded"

    def test_missing_status_treated_as_down(self):
        # The fetcher always sets a status, but if a caller stuffs an
        # entry without one we treat the absence as worst-case.
        services = {"a": {}}
        assert compute_overall(services) == "down"

    def test_empty_services_is_ok(self):
        # Defensive: an empty fan-out (no service URLs configured) is
        # vacuously healthy. The caller is responsible for surfacing this
        # via the empty `services` map.
        assert compute_overall({}) == "ok"


# ── full response builder ────────────────────────────────────────────────────

class TestBuildStatusResponse:
    def _full_fan_out(self):
        return _stub_get({
            "policy-manager":   _FakeResponse(200, {"status": "ok", "redis": True, "timescaledb": True, "policy_version": 29}),
            "autoscaler":       _FakeResponse(200, {"status": "ok", "active_target_count": 3}),
            "telemetry":        _FakeResponse(200, {"status": "ok", "redis": True, "timescaledb": True, "rows_written_1m": 1240}),
            "anomaly-detector": _FakeResponse(200, {"status": "ok", "runloop_enabled": False, "engine": "threshold"}),
            "forecasting":      _FakeResponse(200, {"status": "ok", "runloop_enabled": False, "engine": "moving_average"}),
            "rl-engine":        _FakeResponse(200, {"status": "ok", "runloop_enabled": False, "policy": "random_shadow", "mode": "shadow"}),
            "lb-sidecar":       _FakeResponse(200, {"status": "ok"}),
            "load-balancer":    _FakeResponse(200, {"status": "ok", "upstream_count": 3}),
        })

    def _service_urls(self):
        return {
            "policy-manager":   "http://policy-manager:8086",
            "autoscaler":       "http://autoscaler:8085",
            "telemetry":        "http://telemetry:8081",
            "anomaly-detector": "http://anomaly-detector:8082",
            "forecasting":      "http://forecasting:8083",
            "rl-engine":        "http://rl-engine:8084",
            "lb-sidecar":       "http://lb-sidecar:8087",
            "load-balancer":    "http://load-balancer:80",
        }

    def test_happy_path_all_ok(self):
        response = build_status_response(
            service_urls=self._service_urls(),
            http_get=self._full_fan_out(),
            fetch_active_policy=lambda: {"operating_mode": "hybrid", "safe_mode": False},
            fetch_last_policy_change=lambda: {"actor": "ops", "at": "2026-06-03"},
            fetch_last_scaling_event=lambda: None,
            now_iso=lambda: "2026-06-03T12:00:00Z",
        )
        assert response["overall"] == "ok"
        assert response["generated_at"] == "2026-06-03T12:00:00Z"
        assert set(response["services"].keys()) == set(self._service_urls().keys())
        assert response["active_policy"]["operating_mode"] == "hybrid"
        assert response["recent"]["last_policy_change"]["actor"] == "ops"
        assert response["recent"]["last_scaling_event"] is None

    def test_one_service_down_yields_overall_down(self):
        stub = _stub_get({
            "policy-manager":   _FakeResponse(200, {"status": "ok"}),
            "autoscaler":       _FakeResponse(200, {"status": "ok"}),
            "telemetry":        ConnectionRefusedError("dead"),
            "anomaly-detector": _FakeResponse(200, {"status": "ok"}),
            "forecasting":      _FakeResponse(200, {"status": "ok"}),
            "rl-engine":        _FakeResponse(200, {"status": "ok"}),
            "lb-sidecar":       _FakeResponse(200, {"status": "ok"}),
            "load-balancer":    _FakeResponse(200, {"status": "ok"}),
        })
        response = build_status_response(
            service_urls=self._service_urls(),
            http_get=stub,
            fetch_active_policy=lambda: None,
            fetch_last_policy_change=lambda: None,
            fetch_last_scaling_event=lambda: None,
        )
        assert response["overall"] == "down"
        assert response["services"]["telemetry"]["status"] == "down"
        # Other services unaffected
        assert response["services"]["policy-manager"]["status"] == "ok"

    def test_one_service_degraded_yields_overall_degraded(self):
        stub = _stub_get({
            "policy-manager":   _FakeResponse(200, {"status": "ok"}),
            "autoscaler":       _FakeResponse(200, {"status": "degraded", "reason": "db-slow"}),
            "telemetry":        _FakeResponse(200, {"status": "ok"}),
            "anomaly-detector": _FakeResponse(200, {"status": "ok"}),
            "forecasting":      _FakeResponse(200, {"status": "ok"}),
            "rl-engine":        _FakeResponse(200, {"status": "ok"}),
            "lb-sidecar":       _FakeResponse(200, {"status": "ok"}),
            "load-balancer":    _FakeResponse(200, {"status": "ok"}),
        })
        response = build_status_response(
            service_urls=self._service_urls(),
            http_get=stub,
            fetch_active_policy=lambda: None,
            fetch_last_policy_change=lambda: None,
            fetch_last_scaling_event=lambda: None,
        )
        assert response["overall"] == "degraded"
        assert response["services"]["autoscaler"]["status"] == "degraded"
        assert response["services"]["autoscaler"]["reason"] == "db-slow"

    def test_policy_fetch_failure_does_not_break_response(self):
        def _broken_policy():
            raise RuntimeError("policy-manager unreachable")
        response = build_status_response(
            service_urls=self._service_urls(),
            http_get=self._full_fan_out(),
            fetch_active_policy=_broken_policy,
            fetch_last_policy_change=_broken_policy,
            fetch_last_scaling_event=_broken_policy,
            now_iso=lambda: "X",
        )
        # The response is still well-formed; best-effort fields fall to None.
        assert response["overall"] == "ok"
        assert response["active_policy"] is None
        assert response["recent"]["last_policy_change"] is None
        assert response["recent"]["last_scaling_event"] is None

    def test_empty_service_urls_yields_empty_services_map(self):
        response = build_status_response(
            service_urls={},
            http_get=lambda url, timeout=None: _FakeResponse(200, {"status": "ok"}),
            fetch_active_policy=lambda: None,
            fetch_last_policy_change=lambda: None,
            fetch_last_scaling_event=lambda: None,
        )
        assert response["services"] == {}
        # compute_overall treats {} as "ok" (vacuously) — see TestComputeOverall.
        assert response["overall"] == "ok"

    def test_fan_out_uses_provided_timeout(self):
        captured_timeouts = []

        def _spy(url, timeout=None):
            captured_timeouts.append(timeout)
            return _FakeResponse(200, {"status": "ok"})

        build_status_response(
            service_urls={"a": "http://a", "b": "http://b"},
            http_get=_spy,
            fetch_active_policy=lambda: None,
            fetch_last_policy_change=lambda: None,
            fetch_last_scaling_event=lambda: None,
            timeout_s=0.5,
        )
        assert captured_timeouts == [0.5, 0.5]

    def test_default_timeout_matches_module_constant(self):
        captured = []

        def _spy(url, timeout=None):
            captured.append(timeout)
            return _FakeResponse(200, {"status": "ok"})

        build_status_response(
            service_urls={"a": "http://a"},
            http_get=_spy,
            fetch_active_policy=lambda: None,
            fetch_last_policy_change=lambda: None,
            fetch_last_scaling_event=lambda: None,
        )
        assert captured == [DEFAULT_TIMEOUT_S]
