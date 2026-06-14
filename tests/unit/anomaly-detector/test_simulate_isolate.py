"""
tests/unit/anomaly-detector/test_simulate.py
──────────────────────────────────────────────
Unit tests for the anomaly-detector dry-run endpoint
POST /api/v1/actions/simulate (#146). Exercises the Flask route through a test
client — no Redis, no DB.

Coverage:
  1. Dry-run shape — the synthetic AnomalyEvent envelope (event_id, source,
     version, timestamp, payload) is returned in full, un-published.
  2. Side-effect freedom — no envelope publish, no backend_health write.
  3. Validation PARITY — every body simulate rejects with 400 is the same body
     POST /api/v1/isolate rejects with the same field, and vice-versa.
"""

from __future__ import annotations

import importlib
import sys
import uuid
from pathlib import Path

import pytest

# Load the anomaly-detector service modules with the service dir at the FRONT
# of sys.path, purging any sibling-service modules of the same basename
# (manual / app / decisions) cached by another service's test under pytest's
# prepend import mode. See tests/unit/autoscaler/test_simulate.py for the
# matching purge on the autoscaler side.
_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "anomaly-detector"
for _name in ("app", "manual", "decisions", "cluster_client", "runloop"):
    sys.modules.pop(_name, None)
sys.path.insert(0, str(_SERVICE))

anomaly_app = importlib.import_module("app")


@pytest.fixture
def client(monkeypatch):
    """Trip every side-effect path so an accidental publish/DB write is loud."""
    def _boom_connect(*a, **k):
        raise AssertionError("simulate must not open a DB connection")

    def _boom_redis(*a, **k):
        raise AssertionError("simulate must not connect to Redis")

    monkeypatch.setattr(anomaly_app.psycopg2, "connect", _boom_connect)
    monkeypatch.setattr(anomaly_app.redis_lib, "from_url", _boom_redis)

    anomaly_app.app.config.update(TESTING=True)
    return anomaly_app.app.test_client()


# ── dry-run shape ─────────────────────────────────────────────────────────────

class TestSimulateShape:

    def test_envelope_full_shape(self, client):
        r = client.post("/api/v1/actions/simulate",
                        json={"backend_id": "backend_2", "status": "unhealthy",
                              "actor": "op", "reason": "drill"})
        assert r.status_code == 200
        body = r.get_json()

        assert body["would_publish"] is True
        assert body["channel"] == "smartload.anomaly"
        assert body["backend_id"] == "backend_2"
        assert body["status"] == "unhealthy"
        assert body["severity"] == "critical"
        assert body["reason"] == "manual:op: drill"

        env = body["envelope"]
        # Full envelope shape (event_id, source, version, timestamp, payload).
        assert set(env) == {"event_id", "source", "version", "timestamp", "payload"}
        assert uuid.UUID(env["event_id"])  # parses → valid uuid
        assert env["source"] == "anomaly-detector"
        assert isinstance(env["version"], int)
        assert env["timestamp"]

        payload = env["payload"]
        assert payload["backend_id"] == "backend_2"
        assert payload["status"] == "unhealthy"
        assert payload["score"] == 1.0
        assert payload["severity"] == "critical"
        assert payload["model_version"] == "manual:op"
        assert payload["features"]["reason"] == "manual:op: drill"

    def test_healthy_zero_score_info_severity(self, client):
        body = client.post("/api/v1/actions/simulate",
                           json={"backend_id": "backend_5",
                                 "status": "healthy"}).get_json()
        assert body["severity"] == "info"
        assert body["envelope"]["payload"]["score"] == 0.0

    def test_default_actor_and_reason(self, client):
        body = client.post("/api/v1/actions/simulate",
                           json={"backend_id": "backend_1",
                                 "status": "degraded"}).get_json()
        assert body["reason"] == "manual:operator: manual"

    def test_x_actor_header_used(self, client):
        body = client.post("/api/v1/actions/simulate",
                           json={"backend_id": "backend_1", "status": "degraded"},
                           headers={"X-Actor": "header-op"}).get_json()
        assert body["reason"].startswith("manual:header-op:")

    def test_each_call_mints_fresh_event_id(self, client):
        a = client.post("/api/v1/actions/simulate",
                        json={"backend_id": "b", "status": "unhealthy"}).get_json()
        b = client.post("/api/v1/actions/simulate",
                        json={"backend_id": "b", "status": "unhealthy"}).get_json()
        assert a["envelope"]["event_id"] != b["envelope"]["event_id"]


# ── validation parity with POST /api/v1/isolate ──────────────────────────────

# (body, expected_field). expected_field=None means the body is VALID.
_BODIES = [
    ({"backend_id": "backend_1", "status": "unhealthy"}, None),
    ({"backend_id": "backend_1", "status": "degraded"}, None),
    ({"backend_id": "backend_1", "status": "healthy"}, None),
    ({"backend_id": "", "status": "unhealthy"}, "backend_id"),
    ({"backend_id": "   ", "status": "unhealthy"}, "backend_id"),
    ({"status": "unhealthy"}, "backend_id"),                    # missing backend_id
    ({"backend_id": "backend_1", "status": "bogus"}, "status"),
    ({"backend_id": "backend_1"}, "status"),                    # missing status
]


class TestValidationParity:
    """A failed simulate ⟹ a failed real isolate with the same field, and a
    valid simulate ⟹ a valid real isolate. The real isolate's publish + write
    are stubbed so valid bodies reach 200 without side effects."""

    @pytest.mark.parametrize("body,field", _BODIES)
    def test_simulate_validation(self, client, body, field):
        r = client.post("/api/v1/actions/simulate", json=body)
        if field is None:
            assert r.status_code == 200
        else:
            assert r.status_code == 400
            assert r.get_json().get("field") == field

    @pytest.mark.parametrize("body,field", _BODIES)
    def test_real_isolate_validation_matches(self, monkeypatch, body, field):
        captured = {"published": 0, "rows": 0}

        class _FakeRedis:
            pass

        def _fake_publish(redis_client, *, channel, source, payload):
            captured["published"] += 1
            return "00000000-0000-0000-0000-000000000000"

        class _FakeCursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **k): captured["rows"] += 1

        class _FakeConn:
            def cursor(self): return _FakeCursor()
            def commit(self): pass
            def close(self): pass

        monkeypatch.setattr(anomaly_app.redis_lib, "from_url",
                            lambda *a, **k: _FakeRedis())
        monkeypatch.setattr(anomaly_app, "publish_envelope", _fake_publish)
        monkeypatch.setattr(anomaly_app.psycopg2, "connect",
                            lambda *a, **k: _FakeConn())

        client = anomaly_app.app.test_client()
        r = client.post("/api/v1/isolate", json=body)
        if field is None:
            assert r.status_code == 200
            # Valid body actually published + wrote on the real endpoint.
            assert captured["published"] == 1
            assert captured["rows"] == 1
        else:
            assert r.status_code == 400
            assert r.get_json().get("field") == field
            # Invalid body short-circuits BEFORE any side effect.
            assert captured["published"] == 0
            assert captured["rows"] == 0
