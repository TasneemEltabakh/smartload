"""
tests/unit/autoscaler/test_simulate.py
────────────────────────────────────────
Unit tests for the autoscaler dry-run endpoint POST /api/v1/actions/simulate
(#146). Exercises the Flask route through a test client with the cluster
read stubbed — no Docker, no DB, no Redis.

Coverage:
  1. Dry-run shape — would_execute / current_count / target_count / action /
     cooldown_remaining_s / would_audit_reason / policy_bounds.
  2. Side-effect freedom — no cluster.scale_*, no DB connection, no envelope
     publish; the cooldown clock is unchanged.
  3. Validation PARITY — every body that simulate rejects with 400 is the same
     body that POST /api/v1/scale rejects with the same field, and vice-versa.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# Load the autoscaler service modules with the service dir at the FRONT of
# sys.path, purging any sibling-service modules of the same basename
# (manual / app / decisions) that another service's test may have cached
# under pytest's prepend import mode. This keeps the autoscaler + anomaly
# app-route tests independent within a single pytest session.
_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "autoscaler"
for _name in ("app", "manual", "decisions", "cluster_client", "runloop"):
    sys.modules.pop(_name, None)
sys.path.insert(0, str(_SERVICE))

autoscaler_app = importlib.import_module("app")
from decisions import Policy   # noqa: E402


_POLICY = Policy(
    min_backends=2,
    max_backends=8,
    per_instance_capacity_rps=100.0,
    cooldown_seconds=60.0,
)


class _FakeCluster:
    """Stand-in for DockerClusterClient. Records actuation so the test can
    assert simulate never scales."""

    def __init__(self, count: int):
        self._count = count
        self.scale_out_calls = 0
        self.scale_in_calls = 0

    def get_backend_count(self) -> int:
        return self._count

    def scale_out(self):
        self.scale_out_calls += 1
        self._count += 1
        return ("fake-backend", "toggle")

    def scale_in(self):
        self.scale_in_calls += 1
        self._count -= 1
        return ("fake-backend", "toggle")


@pytest.fixture
def stubbed(monkeypatch):
    """Pin the live policy + a fake cluster, and trip every side-effect path
    so any accidental DB/Redis use is loud rather than silent."""
    cluster = _FakeCluster(count=4)

    monkeypatch.setattr(autoscaler_app, "_policy", _POLICY, raising=False)
    monkeypatch.setattr(autoscaler_app, "_last_action_monotonic", None, raising=False)
    monkeypatch.setattr(autoscaler_app, "_make_cluster_client", lambda: cluster)

    def _boom_connect(*a, **k):
        raise AssertionError("simulate must not open a DB connection")

    def _boom_redis(*a, **k):
        raise AssertionError("simulate must not connect to Redis")

    monkeypatch.setattr(autoscaler_app.psycopg2, "connect", _boom_connect)
    monkeypatch.setattr(autoscaler_app.redis_lib, "from_url", _boom_redis)

    autoscaler_app.app.config.update(TESTING=True)
    client = autoscaler_app.app.test_client()
    return client, cluster


# ── dry-run shape ─────────────────────────────────────────────────────────────

class TestSimulateShape:

    def test_scale_out_preview_shape(self, stubbed):
        client, cluster = stubbed
        r = client.post("/api/v1/actions/simulate",
                        json={"target_count": 6, "actor": "op", "reason": "drill"})
        assert r.status_code == 200
        body = r.get_json()
        assert body == {
            "would_execute":        True,
            "current_count":        4,
            "target_count":         6,
            "action":               "scale_out",
            "cooldown_remaining_s": 0.0,
            "would_audit_reason":   "manual:op: drill",
            "policy_bounds":        {"min_backends": 2, "max_backends": 8},
        }

    def test_scale_in_preview_action(self, stubbed):
        client, _ = stubbed
        body = client.post("/api/v1/actions/simulate",
                           json={"target_count": 2}).get_json()
        assert body["action"] == "scale_in"
        assert body["would_execute"] is True
        assert body["target_count"] == 2

    def test_noop_preview_would_not_execute(self, stubbed):
        client, _ = stubbed
        body = client.post("/api/v1/actions/simulate",
                           json={"target_count": 4}).get_json()
        assert body["action"] == "noop"
        assert body["would_execute"] is False
        assert body["current_count"] == body["target_count"] == 4

    def test_default_actor_and_reason(self, stubbed):
        client, _ = stubbed
        body = client.post("/api/v1/actions/simulate",
                           json={"target_count": 5}).get_json()
        assert body["would_audit_reason"] == "manual:operator: manual override"

    def test_x_actor_header_used(self, stubbed):
        client, _ = stubbed
        body = client.post("/api/v1/actions/simulate",
                           json={"target_count": 5},
                           headers={"X-Actor": "header-op"}).get_json()
        assert body["would_audit_reason"].startswith("manual:header-op:")


# ── side-effect freedom ───────────────────────────────────────────────────────

class TestSimulateNoSideEffects:

    def test_no_cluster_actuation(self, stubbed):
        client, cluster = stubbed
        client.post("/api/v1/actions/simulate", json={"target_count": 8})
        assert cluster.scale_out_calls == 0
        assert cluster.scale_in_calls == 0
        assert cluster.get_backend_count() == 4  # unchanged

    def test_cooldown_clock_unchanged(self, stubbed, monkeypatch):
        client, _ = stubbed
        monkeypatch.setattr(autoscaler_app, "_last_action_monotonic", 123.0,
                            raising=False)
        client.post("/api/v1/actions/simulate", json={"target_count": 6})
        assert autoscaler_app._last_action_monotonic == 123.0


# ── validation parity with POST /api/v1/scale ────────────────────────────────

# (body, expected_field). expected_field=None means the body is VALID and must
# NOT 400 on either endpoint.
_BODIES = [
    ({"target_count": 6}, None),
    ({"target_count": 4}, None),                       # noop, still valid
    ({"target_count": _POLICY.max_backends + 1}, "target_count"),
    ({"target_count": _POLICY.min_backends - 1}, "target_count"),
    ({"target_count": -5}, "target_count"),
    ({"target_count": "not-a-number"}, "target_count"),
    ({}, "target_count"),                              # missing target_count
]


class TestValidationParity:
    """A failed simulate ⟹ a failed real scale with the same field, and a
    valid simulate ⟹ a valid real scale. We stub the real scale's writes so
    the valid cases reach 200 without touching DB/Redis."""

    @pytest.mark.parametrize("body,field", _BODIES)
    def test_simulate_validation(self, stubbed, body, field):
        client, _ = stubbed
        r = client.post("/api/v1/actions/simulate", json=body)
        if field is None:
            assert r.status_code == 200
        else:
            assert r.status_code == 400
            assert r.get_json().get("field") == field

    @pytest.mark.parametrize("body,field", _BODIES)
    def test_real_scale_validation_matches(self, monkeypatch, body, field):
        """Same bodies against POST /api/v1/scale. For valid bodies we let the
        writes run against in-memory fakes so the 200 path is exercised; for
        invalid bodies the 400 must short-circuit BEFORE any write."""
        cluster = _FakeCluster(count=4)
        monkeypatch.setattr(autoscaler_app, "_policy", _POLICY, raising=False)
        monkeypatch.setattr(autoscaler_app, "_last_action_monotonic", None,
                            raising=False)
        monkeypatch.setattr(autoscaler_app, "_make_cluster_client", lambda: cluster)

        class _FakeCursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **k): pass

        class _FakeConn:
            def cursor(self): return _FakeCursor()
            def commit(self): pass
            def close(self): pass

        class _FakeRedis:
            def publish(self, *a, **k): pass

        monkeypatch.setattr(autoscaler_app.psycopg2, "connect",
                            lambda *a, **k: _FakeConn())
        monkeypatch.setattr(autoscaler_app.redis_lib, "from_url",
                            lambda *a, **k: _FakeRedis())

        client = autoscaler_app.app.test_client()
        r = client.post("/api/v1/scale", json=body)
        if field is None:
            assert r.status_code == 200
        else:
            assert r.status_code == 400
            assert r.get_json().get("field") == field


# ── cooldown remaining computation ───────────────────────────────────────────

class TestCooldownRemaining:

    def test_cooldown_remaining_reported(self, stubbed, monkeypatch):
        client, _ = stubbed
        # Pretend an action happened 10s ago against a 60s cooldown.
        monkeypatch.setattr(autoscaler_app.time, "monotonic", lambda: 1000.0)
        monkeypatch.setattr(autoscaler_app, "_last_action_monotonic", 990.0,
                            raising=False)
        body = client.post("/api/v1/actions/simulate",
                           json={"target_count": 6}).get_json()
        assert body["cooldown_remaining_s"] == pytest.approx(50.0)

    def test_cooldown_zero_when_expired(self, stubbed, monkeypatch):
        client, _ = stubbed
        monkeypatch.setattr(autoscaler_app.time, "monotonic", lambda: 2000.0)
        monkeypatch.setattr(autoscaler_app, "_last_action_monotonic", 990.0,
                            raising=False)
        body = client.post("/api/v1/actions/simulate",
                           json={"target_count": 6}).get_json()
        assert body["cooldown_remaining_s"] == 0.0
