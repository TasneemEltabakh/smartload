"""
Tests for app.py features:
  1. _handle_anomaly_message       — updates _anomaly_health from smartload.anomaly envelopes
  2. _pull_initial_policy          — syncs policy from Policy Manager on startup
  3. GET /api/v1/engine/state      — returns SOT §11-compliant engine state shape
  4. _evict_stale_anomaly_health   — C5: drops entries older than the TTL
  5. _refresh_policy_under_lock    — C4: reuses live policy via reload() instead
                                     of reconstructing it on every publish
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── path setup ────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[3]   # smartload/
_SERVICE_ROOT = _ROOT / "services" / "rl-engine"
_PARENT = _ROOT / "services"
for _cand in (_SERVICE_ROOT, _PARENT):
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

def _status_of(backend_id: str) -> str | None:
    """Pull the status out of the (status, monotonic_ts) tuple stored in
    _anomaly_health since the C5 eviction change. Returns None if absent."""
    entry = _app._anomaly_health.get(backend_id)
    return entry[0] if entry is not None else None


class TestHandleAnomalyMessage:
    def setup_method(self):
        _clear_anomaly_health()

    def test_healthy_verdict_stored(self):
        raw = _make_anomaly_raw("backend-1", "healthy")
        _handle_anomaly_message(raw)
        assert _status_of("backend-1") == "healthy"

    def test_degraded_verdict_stored(self):
        raw = _make_anomaly_raw("backend-2", "degraded")
        _handle_anomaly_message(raw)
        assert _status_of("backend-2") == "degraded"

    def test_unhealthy_verdict_stored(self):
        raw = _make_anomaly_raw("backend-3", "unhealthy")
        _handle_anomaly_message(raw)
        assert _status_of("backend-3") == "unhealthy"

    def test_verdict_overwrites_previous(self):
        _handle_anomaly_message(_make_anomaly_raw("backend-1", "healthy"))
        _handle_anomaly_message(_make_anomaly_raw("backend-1", "unhealthy"))
        assert _status_of("backend-1") == "unhealthy"

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
        assert _status_of("b1") == "healthy"
        assert _status_of("b2") == "unhealthy"
        assert set(_app._anomaly_health.keys()) == {"b1", "b2"}


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
        # Reset engine policy to defaults before each test
        with _state_lock:
            _app._engine_policy = _app.EnginePolicy()

    def test_policy_applied_on_success(self):
        with patch("urllib.request.urlopen", return_value=_fake_pm_response(7)):
            _pull_initial_policy()
        assert _app._engine_policy.policy_version == 7

    def test_network_error_leaves_defaults(self):
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            _pull_initial_policy()   # must not raise
        assert _app._engine_policy.policy_version == 0   # default unchanged

    def test_stale_version_not_applied(self):
        # Pre-load a higher version
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
        assert _app._engine_policy.policy_version == 5   # same version, no harm

    def test_malformed_response_leaves_defaults(self):
        resp = MagicMock()
        resp.read.return_value = b"not-json"
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp):
            _pull_initial_policy()   # must not raise
        assert _app._engine_policy.policy_version == 0


# ── 3. GET /api/v1/engine/state ───────────────────────────────────────────────

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

    def test_engine_block_present(self, client):
        data = client.get("/api/v1/engine/state").get_json()
        assert "engine" in data
        eng = data["engine"]
        assert eng["kind"] == "policy"
        assert "requested" in eng
        assert "loaded" in eng
        assert "ready" in eng
        assert "error" in eng

    def test_rl_mode_env_present(self, client):
        data = client.get("/api/v1/engine/state").get_json()
        assert "rl_mode_env" in data
        assert data["rl_mode_env"] == _app.RL_MODE

    def test_runloop_fields_present(self, client):
        data = client.get("/api/v1/engine/state").get_json()
        assert "runloop_enabled" in data
        assert "stats" in data
        stats = data["stats"]
        assert "ticks_total" in stats
        assert "publishes_total" in stats
        assert "last_tick_at" in stats
        assert "last_publish_at" in stats

    def test_last_output_block_present(self, client):
        data = client.get("/api/v1/engine/state").get_json()
        assert "last_output" in data
        lo = data["last_output"]
        # last_output is None until the runloop publishes its first cycle;
        # when populated it carries {mode, server_rankings, policy_version}.
        if lo is not None:
            assert "mode" in lo
            assert "server_rankings" in lo
            assert isinstance(lo["server_rankings"], list)

    def test_tick_counters_are_ints(self, client):
        data = client.get("/api/v1/engine/state").get_json()
        assert isinstance(data["stats"]["ticks_total"], int)
        assert isinstance(data["stats"]["publishes_total"], int)

    def test_runloop_disabled_by_default(self, client):
        # RUNLOOP_ENABLED defaults to False unless env var is set
        data = client.get("/api/v1/engine/state").get_json()
        assert data["runloop_enabled"] is False


# ── 4. C5: _evict_stale_anomaly_health ────────────────────────────────────────

class TestEvictStaleAnomalyHealth:
    def setup_method(self):
        _clear_anomaly_health()

    def test_recent_entries_kept(self):
        _handle_anomaly_message(_make_anomaly_raw("b1", "healthy"))
        # The just-inserted entry is well within TTL.
        _app._evict_stale_anomaly_health(now_monotonic=__import__("time").monotonic())
        assert "b1" in _app._anomaly_health

    def test_old_entries_evicted(self):
        import time as _time
        _handle_anomaly_message(_make_anomaly_raw("b1", "healthy"))
        # Force the entry to look ancient by rewinding its timestamp past
        # the TTL window.
        status, _ts = _app._anomaly_health["b1"]
        _app._anomaly_health["b1"] = (
            status,
            _time.monotonic() - _app._anomaly_health_ttl_seconds - 1.0,
        )
        _app._evict_stale_anomaly_health(now_monotonic=_time.monotonic())
        assert "b1" not in _app._anomaly_health

    def test_eviction_preserves_other_backends(self):
        import time as _time
        _handle_anomaly_message(_make_anomaly_raw("b1", "healthy"))
        _handle_anomaly_message(_make_anomaly_raw("b2", "healthy"))
        # Stale only b1.
        status, _ts = _app._anomaly_health["b1"]
        _app._anomaly_health["b1"] = (
            status,
            _time.monotonic() - _app._anomaly_health_ttl_seconds - 1.0,
        )
        _app._evict_stale_anomaly_health(now_monotonic=_time.monotonic())
        assert "b1" not in _app._anomaly_health
        assert "b2" in _app._anomaly_health


# ── 5. C4: _refresh_policy_under_lock reuses live policy ──────────────────────

class TestRefreshPolicyUnderLock:
    def setup_method(self):
        # Ensure a baseline policy is loaded before each test so the
        # refresh path has something to call reload() on.
        with _state_lock:
            _app._engine_policy = _app.EnginePolicy()
            boot = _app.bootstrap_policy("random_shadow", _app._engine_policy)
            _app._set_policy_state(boot)

    def test_refresh_reuses_existing_policy_instance(self):
        """A new EnginePolicy publish should NOT replace the policy
        instance — reload() updates kwargs in place. Verified by object
        identity: the policy reference before and after must be the
        same object."""
        before = _app._policy
        assert before is not None

        new_policy = _app.EnginePolicy(
            rl_confidence_threshold=0.9,
            rl_exploration_rate=0.1,
            policy_version=1,
        )
        with _state_lock:
            _app._refresh_policy_under_lock(new_policy)

        assert _app._policy is before, (
            "Policy was reconstructed instead of reloaded — C4 regression"
        )
        assert _app._engine_policy.policy_version == 1

    def test_refresh_falls_back_to_bootstrap_when_reload_unsupported(self):
        """If a policy raises NotImplementedError from reload(), the
        refresh path must fall through to bootstrap_policy. This is the
        old behaviour, preserved as a safety net."""
        # Monkey-patch the live policy's reload to refuse the update.
        original_reload = _app._policy.reload
        _app._policy.reload = lambda **kw: (_ for _ in ()).throw(NotImplementedError())
        try:
            with _state_lock:
                _app._refresh_policy_under_lock(
                    _app.EnginePolicy(policy_version=2)
                )
            # The reload refused → bootstrap rebuilt the policy instance.
            assert _app._policy is not None
        finally:
            # Restore so other tests don't inherit the broken reload.
            with _state_lock:
                boot = _app.bootstrap_policy("random_shadow", _app._engine_policy)
                _app._set_policy_state(boot)
