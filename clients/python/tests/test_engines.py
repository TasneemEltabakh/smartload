"""
clients/python/tests/test_engines.py
─────────────────────────────────────
Unit tests for the SDK Live Engines surface (slice #121, session 2).

Coverage:
  1. snapshot()       — happy path, error mapping.
  2. state(service)   — happy path for each service, unknown-service guard,
                        URL routing.
  3. subscribe()      — SSE drain, channel filter, callback exception
                        swallow, close() stops the thread.

No live BFF / no Docker. The HTTP path is mocked via httpx's MockTransport
or a small stand-in URL handler; the SSE path is exercised via a fake
file-like response object.
"""

from __future__ import annotations

import io
import json
import threading
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from smartload_client import SmartLoadClient, engines as engines_mod
from smartload_client.engines import EnginesClient, EnginesSubscription


# Reference to the unpatched httpx.Client so the mock factory doesn't recurse
# into itself when test code creates a client wrapping a MockTransport.
_REAL_HTTPX_CLIENT = httpx.Client


def _mock_httpx_factory(handler):
    """Return a callable that creates a real httpx.Client wired to the
    handler. Used with patch.object(engines_mod, 'httpx', ...) so the
    EnginesClient module hits the mock transport."""
    def _factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kwargs)
    return _factory


class _MockHttpx:
    """Tiny stand-in exposing only `Client` so the engines module's
    `httpx.Client(...)` call site routes through us. Reuses the real
    Response class so production code paths work unchanged."""
    def __init__(self, handler):
        self._handler = handler
    @property
    def Client(self):
        return _mock_httpx_factory(self._handler)
    Response = httpx.Response                          # passthrough for type checks


# ── helpers ───────────────────────────────────────────────────────────────────


def _client(operator_ui_url: str = "http://test-ui:8090") -> SmartLoadClient:
    return SmartLoadClient(
        base_url="http://test-policy:8086",
        autoscaler_url="http://test-autoscaler:8085",
        anomaly_detector_url="http://test-anomaly:8082",
        forecasting_url="http://test-forecast:8083",
        rl_engine_url="http://test-rl:8084",
        operator_ui_url=operator_ui_url,
        redis_url="redis://test:6379",
    )


class _MockResponse:
    """Stand-in for urllib's HTTPResponse for SSE testing — readline() until
    we run out of buffered lines."""

    def __init__(self, lines: list[bytes]) -> None:
        self._buf = io.BytesIO(b"".join(lines))
        self.closed = False

    def readline(self) -> bytes:
        if self.closed:
            return b""
        return self._buf.readline()

    def close(self) -> None:
        self.closed = True


# ── snapshot() ────────────────────────────────────────────────────────────────


class TestSnapshot:
    def test_happy_path_returns_aggregated_body(self):
        client = _client()
        body = {
            "services": {
                "anomaly-detector": {"engine": {"loaded": "threshold"}},
                "forecasting":      {"engine": {"loaded": "arima"}},
                "rl-engine":        {"engine": {"loaded": "ppo"}},
            },
            "channels": {
                "smartload.anomaly":  [],
                "smartload.forecast": [],
                "smartload.routing":  [],
                "smartload.scale":    [],
            },
            "recent": [],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "test-ui"
            assert request.url.path == "/api/ui/engines/snapshot"
            return httpx.Response(200, json=body)

        with patch.object(engines_mod, "httpx", _MockHttpx(handler)):
            result = client.engines.snapshot()

        assert result == body
        assert set(result["services"]) == {
            "anomaly-detector", "forecasting", "rl-engine",
        }

    def test_propagates_500_as_smartload_error(self):
        from smartload_client import SmartLoadError
        client = _client()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "BFF down"})

        with patch.object(engines_mod, "httpx", _MockHttpx(handler)):
            with pytest.raises(SmartLoadError, match="BFF down"):
                client.engines.snapshot()


# ── state(service) ────────────────────────────────────────────────────────────


class TestState:
    @pytest.mark.parametrize(
        "service,expected_host",
        [
            ("anomaly-detector", "test-anomaly"),
            ("forecasting",      "test-forecast"),
            ("rl-engine",        "test-rl"),
        ],
    )
    def test_routes_to_per_service_url(self, service, expected_host):
        client = _client()
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["host"] = request.url.host
            captured["path"] = request.url.path
            return httpx.Response(200, json={"engine": {"loaded": "test"}})

        with patch.object(engines_mod, "httpx", _MockHttpx(handler)):
            client.engines.state(service)

        assert captured["host"] == expected_host
        assert captured["path"] == "/api/v1/engine/state"

    def test_unknown_service_raises_validation_error(self):
        from smartload_client import ValidationError
        client = _client()
        with pytest.raises(ValidationError, match="unknown engine service"):
            client.engines.state("not-a-real-service")


# ── subscribe() ───────────────────────────────────────────────────────────────


class TestSubscribe:
    def _make_sse_lines(self, events: list[dict]) -> list[bytes]:
        """Build a synthetic SSE response from a list of (channel, payload) dicts."""
        lines: list[bytes] = []
        for ev in events:
            lines.append(f"event: {ev['channel']}\n".encode())
            data = json.dumps({
                "channel": ev["channel"],
                "envelope": {
                    "event_id": ev.get("event_id", "abc"),
                    "source":   ev.get("source", "test"),
                    "version":  1,
                    "timestamp": "2026-05-29T00:00:00+00:00",
                    "payload":  ev.get("payload", {}),
                },
                "payload": ev.get("payload", {}),
            })
            lines.append(f"data: {data}\n".encode())
            lines.append(b"\n")                  # event delimiter
        return lines

    def test_drains_data_lines_into_callback(self):
        client = _client()
        events = [
            {"channel": "smartload.anomaly",
             "payload": {"backend_id": "b1", "status": "degraded"}},
            {"channel": "smartload.forecast",
             "payload": {"predicted_rps": 35.0}},
            {"channel": "smartload.routing",
             "payload": {"mode": "shadow"}},
        ]
        sse_response = _MockResponse(self._make_sse_lines(events))

        received: list[tuple[str, dict, dict]] = []

        def cb(channel, payload, meta):
            received.append((channel, payload, meta))

        with patch("urllib.request.urlopen", return_value=sse_response):
            sub = client.engines.subscribe(cb)
            # Wait briefly for the drain thread to exhaust the response.
            sub._thread.join(timeout=2.0)

        sub.close()
        channels = [r[0] for r in received]
        assert channels == [
            "smartload.anomaly", "smartload.forecast", "smartload.routing",
        ]
        # Payloads passed through verbatim
        assert received[0][1] == {"backend_id": "b1", "status": "degraded"}
        # Envelope meta excludes payload
        assert "payload" not in received[0][2]
        assert received[0][2]["source"] == "test"

    def test_channel_filter_skips_unwanted_channels(self):
        client = _client()
        events = [
            {"channel": "smartload.anomaly",  "payload": {"v": 1}},
            {"channel": "smartload.forecast", "payload": {"v": 2}},
            {"channel": "smartload.routing",  "payload": {"v": 3}},
            {"channel": "smartload.scale",    "payload": {"v": 4}},
        ]
        sse_response = _MockResponse(self._make_sse_lines(events))
        received: list[str] = []

        with patch("urllib.request.urlopen", return_value=sse_response):
            sub = client.engines.subscribe(
                lambda ch, p, m: received.append(ch),
                channels=["smartload.routing", "smartload.scale"],
            )
            sub._thread.join(timeout=2.0)

        sub.close()
        assert received == ["smartload.routing", "smartload.scale"]

    def test_callback_exception_does_not_kill_thread(self):
        client = _client()
        events = [
            {"channel": "smartload.anomaly", "payload": {"v": 1}},
            {"channel": "smartload.anomaly", "payload": {"v": 2}},
        ]
        sse_response = _MockResponse(self._make_sse_lines(events))
        seen: list[int] = []

        def bad_cb(channel, payload, meta):
            seen.append(payload["v"])
            if payload["v"] == 1:
                raise RuntimeError("boom")

        with patch("urllib.request.urlopen", return_value=sse_response):
            sub = client.engines.subscribe(bad_cb)
            sub._thread.join(timeout=2.0)

        sub.close()
        # Both events delivered — the raise on v=1 was swallowed by the
        # drain loop instead of killing the thread.
        assert seen == [1, 2]

    def test_heartbeat_comment_skipped(self):
        client = _client()
        # Heartbeat: a line that starts with ":" before any data line.
        sse_response = _MockResponse([
            b": heartbeat\n",
            b"event: smartload.anomaly\n",
            b'data: {"channel": "smartload.anomaly", "payload": {"v": 1}}\n',
            b"\n",
        ])
        received: list[str] = []

        with patch("urllib.request.urlopen", return_value=sse_response):
            sub = client.engines.subscribe(
                lambda ch, p, m: received.append(ch),
            )
            sub._thread.join(timeout=2.0)

        sub.close()
        assert received == ["smartload.anomaly"]

    def test_malformed_data_line_skipped(self):
        client = _client()
        sse_response = _MockResponse([
            b"data: not valid json\n",
            b"\n",
            b"event: smartload.anomaly\n",
            b'data: {"channel": "smartload.anomaly", "payload": {"v": 1}}\n',
            b"\n",
        ])
        received: list[dict] = []

        with patch("urllib.request.urlopen", return_value=sse_response):
            sub = client.engines.subscribe(
                lambda ch, p, m: received.append(p),
            )
            sub._thread.join(timeout=2.0)

        sub.close()
        # Only the well-formed event makes it through.
        assert received == [{"v": 1}]

    def test_close_stops_subscription(self):
        client = _client()
        sse_response = _MockResponse([b": heartbeat\n"] * 10)  # all comments

        with patch("urllib.request.urlopen", return_value=sse_response):
            sub = client.engines.subscribe(lambda ch, p, m: None)
            sub.close()

        assert sub._stop.is_set()


# ── convenience methods on the top-level client ───────────────────────────────


class TestTopLevelConvenience:
    def test_engines_snapshot_delegates(self):
        client = _client()
        with patch.object(client.engines, "snapshot", return_value={"x": 1}) as m:
            assert client.engines_snapshot() == {"x": 1}
            m.assert_called_once()

    def test_engines_state_delegates(self):
        client = _client()
        with patch.object(client.engines, "state", return_value={"x": 1}) as m:
            assert client.engines_state("rl-engine") == {"x": 1}
            m.assert_called_once_with("rl-engine")

    def test_subscribe_anomaly_filters_to_one_channel(self):
        client = _client()
        cb = lambda *a: None
        with patch.object(client.engines, "subscribe") as m:
            client.subscribe_anomaly(cb)
            m.assert_called_once_with(cb, channels=["smartload.anomaly"])
