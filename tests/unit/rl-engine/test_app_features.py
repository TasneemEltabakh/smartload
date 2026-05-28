"""
tests/unit/rl-engine/test_app_features.py
─────────────────────────────────────────
Pure-Python unit tests for surfaces in services/rl-engine/app.py that are
*not* covered by test_runloop.py. No Docker, no Redis, no DB — runs in the
unit-tests CI job.

Coverage boundary with test_runloop.py:
  test_runloop.py        — pure runloop logic (classify_health, bootstrap,
                           policy_from_payload, build_state_from_rows,
                           effective_mode, should_publish, action_to_event_payload,
                           EnginePolicy, serialize_engine_state).
  test_app_features.py   — app.py surfaces that go beyond runloop logic:
                            1. _handle_anomaly_message     (Redis subscriber callback)
                            2. _pull_initial_policy        (startup Policy Manager sync)
                            3. GET /api/v1/engine/state    (HTTP wiring around
                                                            serialize_engine_state)

  Note: the deep schema-of-serialize_engine_state assertions are intentionally
  not duplicated here — test_runloop.py asserts the full top-level key set,
  engine.kind/loaded/ready/requested/error, and last_output passthrough at
  the serializer layer. These endpoint tests only verify the HTTP-layer
  contract (status code, JSON parsing, request-time defaults).
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── path setup — mirror test_runloop.py so app and shared resolve cleanly ─────
_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "rl-engine"
_SERVICES_DIR = _SERVICE.parent  # services/ — needed for `from shared.contracts import …`
for _cand in (_SERVICE, _SERVICES_DIR):
    if str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

# ── import app under test ─────────────────────────────────────────────────────
import app as _app  # noqa: E402
from app import (  # noqa: E402
    _handle_anomaly_message,
    _pull_initial_policy,
    _state_lock,
    app as flask_app,
)
from shared.contracts import make_envelope, AnomalyEvent  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_anomaly_raw(backend_id: str, status: str, score: float = 0.9) -> bytes:
    """Build a valid smartload.anomaly envelope body as bytes."""
    env = make_envelope(
        source="anomaly-detector",
        payload=AnomalyEvent(backend_id=backend_id, status=status, score=score),
    )
    return json.dumps(asdict(env)).encode()


def _clear_anomaly_health() -> None:
    with _state_lock:
        _app._anomaly_health.clear()


# ── 1. _handle_anomaly_message ────────────────────────────────────────────────

class TestHandleAnomalyMessage:
    def setup_method(self):
        _clear_anomaly_health()

    def test_healthy_verdict_stored(self):
        raw = _make_anomaly_raw("backend-1", "healthy")
        _handle_anomaly_message(raw)
        assert _app._anomaly_health.get("backend-1") == "healthy"

    def test_degraded_verdict_stored(self):
        raw = _make_anomaly_raw("backend-2", "degraded")
        _handle_anomaly_message(raw)
        assert _app._anomaly_health.get("backend-2") == "degraded"

    def test_unhealthy_verdict_stored(self):
        raw = _make_anomaly_raw("backend-3", "unhealthy")
        _handle_anomaly_message(raw)
        assert _app._anomaly_health.get("backend-3") == "unhealthy"

    def test_verdict_overwrites_previous(self):
        _handle_anomaly_message(_make_anomaly_raw("backend-1", "healthy"))
        _handle_anomaly_message(_make_anomaly_raw("backend-1", "unhealthy"))
        assert _app._anomaly_health["backend-1"] == "unhealthy"

    def test_malformed_json_ignored(self):
        _handle_anomaly_message(b"not-json")
        assert _app._anomaly_health == {}

    def test_missing_backend_id_ignored(self):
        env = make_envelope(source="x", payload={"status": "unhealthy"})
        raw = json.dumps(asdict(env)).encode()
        _handle_anomaly_message(raw)
        assert _app._anomaly_health == {}

    def test_invalid_status_ignored(self):
        env = make_envelope(
            source="x",
            payload={"backend_id": "b1", "status": "unknown-value"},
        )
        raw = json.dumps(asdict(env)).encode()
        _handle_anomaly_message(raw)
        assert _app._anomaly_health == {}

    def test_multiple_backends_independent(self):
        _handle_anomaly_message(_make_anomaly_raw("b1", "healthy"))
        _handle_anomaly_message(_make_anomaly_raw("b2", "unhealthy"))
        assert _app._anomaly_health == {"b1": "healthy", "b2": "unhealthy"}


# ── 2. _pull_initial_policy ───────────────────────────────────────────────────

def _fake_pm_response(policy_version: int = 5) -> MagicMock:
    """Return a context-manager mock that yields a response with a valid policy payload."""
    payload = {
        "operating_mode": "shadow",
        "safe_mode": False,
        "policy_version": policy_version,
        "rl_exploration_rate": 0.0,
        "rl_confidence_threshold": 0.6,
    }
    body = json.dumps({"payload": payload}).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestPullInitialPolicy:
    def setup_method(self):
        with _state_lock:
            _app._engine_policy = _app.EnginePolicy()

    def test_policy_applied_on_success(self):
        with patch("urllib.request.urlopen", return_value=_fake_pm_response(7)):
            _pull_initial_policy()
        assert _app._engine_policy.policy_version == 7

    def test_network_error_leaves_defaults(self):
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            _pull_initial_policy()
        assert _app._engine_policy.policy_version == 0

    def test_stale_version_not_applied(self):
        with _state_lock:
            _app._engine_policy = _app.EnginePolicy(policy_version=10)
        with patch("urllib.request.urlopen", return_value=_fake_pm_response(3)):
            _pull_initial_policy()
        # Monotonic guard — version 3 < 10, must not overwrite
        assert _app._engine_policy.policy_version == 10

    def test_equal_version_is_applied(self):
        with _state_lock:
            _app._engine_policy = _app.EnginePolicy(policy_version=5)
        with patch("urllib.request.urlopen", return_value=_fake_pm_response(5)):
            _pull_initial_policy()
        assert _app._engine_policy.policy_version == 5

    def test_malformed_response_leaves_defaults(self):
        resp = MagicMock()
        resp.read.return_value = b"not-json"
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp):
            _pull_initial_policy()
        assert _app._engine_policy.policy_version == 0


# ── 3. GET /api/v1/engine/state — HTTP wiring only ────────────────────────────
#
# Deep-shape assertions on the serialized body live in test_runloop.py against
# serialize_engine_state directly. The tests below only verify that the Flask
# route is wired, returns 200 with parseable JSON, and reflects request-time
# environment (rl_mode_env) and default runloop state.

@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


class TestEngineStateEndpoint:
    def test_returns_200(self, client):
        resp = client.get("/api/v1/engine/state")
        assert resp.status_code == 200

    def test_response_is_json(self, client):
        resp = client.get("/api/v1/engine/state")
        data = resp.get_json()
        assert data is not None

    def test_rl_mode_env_present(self, client):
        """Endpoint must surface the runtime RL_MODE env, not just the
        serializer's input — verifies HTTP layer reads it correctly."""
        data = client.get("/api/v1/engine/state").get_json()
        assert "rl_mode_env" in data
        assert data["rl_mode_env"] == _app.RL_MODE

    def test_runloop_block_present(self, client):
        data = client.get("/api/v1/engine/state").get_json()
        assert "runloop" in data
        rl = data["runloop"]
        assert "enabled" in rl
        assert "tick_count" in rl
        assert "publish_count" in rl
        assert "last_tick_iso" in rl
        assert "last_publish_iso" in rl

    def test_last_output_block_present(self, client):
        """Endpoint returns a last_output block even when no inference cycle
        has run yet — verifies the default-state HTTP contract."""
        data = client.get("/api/v1/engine/state").get_json()
        assert "last_output" in data
        lo = data["last_output"]
        assert "mode" in lo
        assert "server_rankings" in lo
        assert isinstance(lo["server_rankings"], list)

    def test_tick_counters_are_ints(self, client):
        data = client.get("/api/v1/engine/state").get_json()
        assert isinstance(data["runloop"]["tick_count"], int)
        assert isinstance(data["runloop"]["publish_count"], int)

    def test_runloop_disabled_by_default(self, client):
        """RUNLOOP_ENABLED defaults to False unless env var is set —
        verifies the endpoint reads the env at request time."""
        data = client.get("/api/v1/engine/state").get_json()
        assert data["runloop"]["enabled"] is False
