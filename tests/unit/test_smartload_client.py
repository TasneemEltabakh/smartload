"""
tests/unit/test_smartload_client.py
────────────────────────────────────
Pure-function unit tests for the SmartLoad Python client.

These tests do NOT require a running stack. They cover:
  - _raise_for_status status-code → exception mapping
  - parse_envelope round-trip + bad-input rejection + staleness handling

The HTTP/Redis layers are tested via the e2e suite at
tests/e2e/policy-management/.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_SDK_ROOT = Path(__file__).resolve().parents[2] / "clients" / "python"
if str(_SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(_SDK_ROOT))

from smartload_client.exceptions import (  # noqa: E402
    AuthenticationError,
    RateLimitError,
    SmartLoadError,
    ValidationError,
)
from smartload_client._envelope import (  # noqa: E402
    CHANNEL_ANOMALY,
    CHANNEL_POLICY,
    parse_envelope,
)
from smartload_client.policy import _raise_for_status  # noqa: E402


def _fake_response(status_code: int, *, json_body=None, text="", headers=None):
    """Minimal stand-in for httpx.Response that satisfies _raise_for_status."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = headers or {}
    if json_body is None:
        resp.json.side_effect = ValueError("no body")
    else:
        resp.json.return_value = json_body
    return resp


# ── _raise_for_status ─────────────────────────────────────────────────────────

class TestRaiseForStatus:

    def test_2xx_does_not_raise(self):
        _raise_for_status(_fake_response(200, json_body={}))
        _raise_for_status(_fake_response(204))

    def test_400_maps_to_validation_error_with_field(self):
        resp = _fake_response(
            400, json_body={"error": "min_backends must be > 0", "field": "min_backends"},
        )
        with pytest.raises(ValidationError) as exc:
            _raise_for_status(resp)
        assert exc.value.field == "min_backends"
        assert "min_backends" in str(exc.value)

    def test_400_without_field_still_raises_validation_error(self):
        resp = _fake_response(400, json_body={"error": "bad request"})
        with pytest.raises(ValidationError) as exc:
            _raise_for_status(resp)
        assert exc.value.field is None

    def test_401_maps_to_authentication_error(self):
        resp = _fake_response(401, json_body={"error": "missing token"})
        with pytest.raises(AuthenticationError):
            _raise_for_status(resp)

    def test_403_maps_to_authentication_error(self):
        resp = _fake_response(403, json_body={"error": "scope not granted"})
        with pytest.raises(AuthenticationError):
            _raise_for_status(resp)

    def test_429_populates_retry_after(self):
        resp = _fake_response(
            429, json_body={"error": "slow down"}, headers={"Retry-After": "12"},
        )
        with pytest.raises(RateLimitError) as exc:
            _raise_for_status(resp)
        assert exc.value.retry_after == 12

    def test_429_without_retry_after_header_still_raises(self):
        resp = _fake_response(429, json_body={"error": "slow down"})
        with pytest.raises(RateLimitError) as exc:
            _raise_for_status(resp)
        assert exc.value.retry_after is None

    def test_5xx_maps_to_generic_smartload_error(self):
        resp = _fake_response(500, json_body={"error": "kaboom"})
        with pytest.raises(SmartLoadError) as exc:
            _raise_for_status(resp)
        # Must not be one of the more specific subclasses
        assert not isinstance(exc.value, (ValidationError, AuthenticationError, RateLimitError))

    def test_non_json_body_falls_back_to_text(self):
        resp = _fake_response(502, text="bad gateway")
        with pytest.raises(SmartLoadError) as exc:
            _raise_for_status(resp)
        assert "502" in str(exc.value)


# ── parse_envelope ────────────────────────────────────────────────────────────

def _envelope(payload: dict, timestamp: str | None = None) -> str:
    return json.dumps({
        "event_id": "e1",
        "source": "policy-manager",
        "version": 1,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    })


class TestParseEnvelope:

    def test_round_trip_returns_payload_and_meta(self):
        raw = _envelope({"safe_mode": True, "policy_version": 7})
        parsed = parse_envelope(raw, channel=CHANNEL_POLICY)
        assert parsed is not None
        payload, meta = parsed
        assert payload == {"safe_mode": True, "policy_version": 7}
        assert meta["event_id"] == "e1"
        assert meta["source"] == "policy-manager"
        assert "payload" not in meta

    def test_bytes_input_is_decoded(self):
        raw = _envelope({"safe_mode": False}).encode("utf-8")
        parsed = parse_envelope(raw, channel=CHANNEL_POLICY)
        assert parsed is not None
        payload, _meta = parsed
        assert payload == {"safe_mode": False}

    def test_malformed_json_returns_none(self):
        assert parse_envelope("{not json", channel=CHANNEL_POLICY) is None

    def test_missing_payload_field_returns_none(self):
        raw = json.dumps({"event_id": "e1", "timestamp": "2026-05-14T12:00:00+00:00"})
        assert parse_envelope(raw, channel=CHANNEL_POLICY) is None

    def test_missing_timestamp_returns_none(self):
        raw = json.dumps({"event_id": "e1", "payload": {}})
        assert parse_envelope(raw, channel=CHANNEL_POLICY) is None

    def test_stale_message_on_ttl_channel_returns_none(self):
        old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        raw = _envelope({"backend_id": "b1", "status": "degraded"}, timestamp=old)
        # CHANNEL_ANOMALY TTL is 30s; 120s is stale.
        assert parse_envelope(raw, channel=CHANNEL_ANOMALY) is None

    def test_stale_message_on_no_ttl_channel_is_accepted(self):
        # CHANNEL_POLICY has no TTL — even ancient messages are accepted.
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        raw = _envelope({"safe_mode": True}, timestamp=old)
        parsed = parse_envelope(raw, channel=CHANNEL_POLICY)
        assert parsed is not None

    def test_naive_timestamp_on_ttl_channel_returns_none(self):
        # No timezone info — must be rejected on a TTL-bearing channel.
        naive = datetime.now().isoformat()  # no tz
        raw = _envelope({"backend_id": "b1"}, timestamp=naive)
        assert parse_envelope(raw, channel=CHANNEL_ANOMALY) is None

    def test_unparseable_timestamp_on_ttl_channel_returns_none(self):
        raw = _envelope({"backend_id": "b1"}, timestamp="not-a-date")
        assert parse_envelope(raw, channel=CHANNEL_ANOMALY) is None


# ── AuditClient (slice #2, #122) ──────────────────────────────────────────────

from smartload_client.audit import AuditClient  # noqa: E402
from smartload_client.client import SmartLoadClient  # noqa: E402


class TestListAuditDispatch:
    """The list(kind) method routes to the right per-kind helper.

    The actual HTTP calls are mocked — these tests cover dispatch logic +
    bad-kind validation, not the wire protocol (e2e covers that)."""

    def test_unknown_kind_raises_validation_error(self):
        client = MagicMock()
        ac = AuditClient(client)
        with pytest.raises(ValidationError) as exc:
            ac.list("not-a-kind")     # type: ignore[arg-type]
        assert exc.value.field == "kind"

    def test_policy_kind_calls_policy_helper(self):
        client = MagicMock()
        client._http.get.return_value = _fake_response(200, json_body=[{"time": "t1"}])
        ac = AuditClient(client)
        rows = ac.list("policy", limit=10)
        assert rows == [{"time": "t1"}]
        client._http.get.assert_called_once_with(
            "/api/v1/audit/policy", params={"limit": 10},
        )

    def test_scaling_kind_targets_autoscaler_url(self, monkeypatch):
        """Scaling audit lives on a different upstream than policy audit —
        must call autoscaler_url, not the policy-manager base_url."""
        client = MagicMock()
        client.autoscaler_url = "http://my-autoscaler:8085"
        client.timeout = 5.0
        captured = {}

        def fake_get(url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            captured["timeout"] = timeout
            return _fake_response(200, json_body=[{"action": "scale_out"}])

        monkeypatch.setattr("smartload_client.audit.httpx.get", fake_get)
        ac = AuditClient(client)
        rows = ac.list("scaling", limit=25)
        assert rows == [{"action": "scale_out"}]
        assert captured["url"] == "http://my-autoscaler:8085/api/v1/audit/scaling"
        assert captured["params"] == {"limit": 25}
        assert captured["timeout"] == 5.0


class TestSmartLoadClientWiring:
    """The top-level client correctly exposes the audit sub-client +
    accepts the new autoscaler_url parameter."""

    def test_audit_subclient_is_attached(self):
        c = SmartLoadClient(base_url="http://example:8086")
        assert isinstance(c.audit, AuditClient)
        c.close()

    def test_autoscaler_url_defaults_to_localhost(self):
        c = SmartLoadClient(base_url="http://example:8086")
        assert c.autoscaler_url == "http://localhost:8085"
        c.close()

    def test_autoscaler_url_strips_trailing_slash(self):
        c = SmartLoadClient(
            base_url="http://example:8086",
            autoscaler_url="http://my-autoscaler:8085/",
        )
        assert c.autoscaler_url == "http://my-autoscaler:8085"
        c.close()

    def test_autoscaler_url_picks_up_env_var(self, monkeypatch):
        monkeypatch.setenv("SMARTLOAD_AUTOSCALER_URL", "http://env-autoscaler:9999")
        c = SmartLoadClient(base_url="http://example:8086")
        assert c.autoscaler_url == "http://env-autoscaler:9999"
        c.close()

    def test_list_audit_delegates_to_audit_subclient(self, monkeypatch):
        c = SmartLoadClient(base_url="http://example:8086")
        captured = {}

        def fake_list(kind, limit=50):
            captured["kind"] = kind
            captured["limit"] = limit
            return [{"ok": True}]

        monkeypatch.setattr(c.audit, "list", fake_list)
        rows = c.list_audit("scaling", limit=7)
        assert rows == [{"ok": True}]
        assert captured == {"kind": "scaling", "limit": 7}
        c.close()


# ── ActionsClient (slice #3, #123) ────────────────────────────────────────────

from smartload_client.actions import ActionsClient  # noqa: E402


class TestActionsScale:

    def test_scale_targets_autoscaler_url(self, monkeypatch):
        client = MagicMock()
        client.autoscaler_url = "http://my-autoscaler:8085"
        client.timeout = 5.0
        client.default_actor = "smartload-client"
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return _fake_response(200, json_body={"status": "applied"})

        monkeypatch.setattr("smartload_client.actions.httpx.post", fake_post)
        ac = ActionsClient(client)
        result = ac.scale(3, actor="alice", reason="failover drill")
        assert result == {"status": "applied"}
        assert captured["url"] == "http://my-autoscaler:8085/api/v1/scale"
        assert captured["json"] == {
            "target_count": 3,
            "actor": "alice",
            "reason": "failover drill",
        }

    def test_scale_defaults_actor_to_client_default(self, monkeypatch):
        client = MagicMock()
        client.autoscaler_url = "http://x:8085"
        client.timeout = 5.0
        client.default_actor = "my-tool"
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["json"] = json
            return _fake_response(200, json_body={"status": "applied"})

        monkeypatch.setattr("smartload_client.actions.httpx.post", fake_post)
        ac = ActionsClient(client)
        ac.scale(2)
        assert captured["json"]["actor"] == "my-tool"
        assert "reason" not in captured["json"]   # opt-in field

    def test_scale_400_raises_validation_error(self, monkeypatch):
        client = MagicMock()
        client.autoscaler_url = "http://x:8085"
        client.timeout = 5.0
        client.default_actor = "smartload-client"

        def fake_post(url, json=None, timeout=None):
            return _fake_response(
                400,
                json_body={
                    "error": "target_count 9 above policy.max_backends (5)",
                    "field": "target_count",
                },
            )

        monkeypatch.setattr("smartload_client.actions.httpx.post", fake_post)
        ac = ActionsClient(client)
        with pytest.raises(ValidationError) as exc:
            ac.scale(9)
        assert exc.value.field == "target_count"


class TestActionsIsolate:

    def test_isolate_targets_anomaly_detector_url(self, monkeypatch):
        client = MagicMock()
        client.anomaly_detector_url = "http://my-anomaly:8082"
        client.timeout = 5.0
        client.default_actor = "smartload-client"
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return _fake_response(200, json_body={"status": "applied"})

        monkeypatch.setattr("smartload_client.actions.httpx.post", fake_post)
        ac = ActionsClient(client)
        result = ac.isolate("backend_1", "unhealthy", actor="bob", reason="manual")
        assert result == {"status": "applied"}
        assert captured["url"] == "http://my-anomaly:8082/api/v1/isolate"
        assert captured["json"] == {
            "backend_id": "backend_1",
            "status": "unhealthy",
            "actor": "bob",
            "reason": "manual",
        }

    def test_isolate_default_status_is_unhealthy(self, monkeypatch):
        client = MagicMock()
        client.anomaly_detector_url = "http://x:8082"
        client.timeout = 5.0
        client.default_actor = "smartload-client"
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["json"] = json
            return _fake_response(200, json_body={"status": "applied"})

        monkeypatch.setattr("smartload_client.actions.httpx.post", fake_post)
        ac = ActionsClient(client)
        ac.isolate("backend_1")
        assert captured["json"]["status"] == "unhealthy"


class TestActionsSimulate:
    """Dry-run SDK surface (#146). Same wiring as scale()/isolate() but
    targeting /api/v1/actions/simulate on the respective upstream."""

    def test_simulate_scale_targets_autoscaler_url(self, monkeypatch):
        client = MagicMock()
        client.autoscaler_url = "http://my-autoscaler:8085"
        client.timeout = 5.0
        client.default_actor = "smartload-client"
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return _fake_response(200, json_body={"would_execute": True})

        monkeypatch.setattr("smartload_client.actions.httpx.post", fake_post)
        ac = ActionsClient(client)
        result = ac.simulate_scale(3, actor="alice", reason="drill")
        assert result == {"would_execute": True}
        assert captured["url"] == "http://my-autoscaler:8085/api/v1/actions/simulate"
        assert captured["json"] == {
            "target_count": 3,
            "actor": "alice",
            "reason": "drill",
        }

    def test_simulate_scale_400_raises_validation_error(self, monkeypatch):
        client = MagicMock()
        client.autoscaler_url = "http://x:8085"
        client.timeout = 5.0
        client.default_actor = "smartload-client"

        def fake_post(url, json=None, timeout=None):
            return _fake_response(
                400,
                json_body={"error": "out of bounds", "field": "target_count"},
            )

        monkeypatch.setattr("smartload_client.actions.httpx.post", fake_post)
        ac = ActionsClient(client)
        with pytest.raises(ValidationError) as exc:
            ac.simulate_scale(99)
        assert exc.value.field == "target_count"

    def test_simulate_isolate_targets_anomaly_detector_url(self, monkeypatch):
        client = MagicMock()
        client.anomaly_detector_url = "http://my-anomaly:8082"
        client.timeout = 5.0
        client.default_actor = "smartload-client"
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return _fake_response(200, json_body={"would_publish": True})

        monkeypatch.setattr("smartload_client.actions.httpx.post", fake_post)
        ac = ActionsClient(client)
        result = ac.simulate_isolate("backend_1", "degraded", actor="bob")
        assert result == {"would_publish": True}
        assert captured["url"] == "http://my-anomaly:8082/api/v1/actions/simulate"
        assert captured["json"] == {
            "backend_id": "backend_1",
            "status": "degraded",
            "actor": "bob",
        }

    def test_simulate_isolate_default_status_is_unhealthy(self, monkeypatch):
        client = MagicMock()
        client.anomaly_detector_url = "http://x:8082"
        client.timeout = 5.0
        client.default_actor = "smartload-client"
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["json"] = json
            return _fake_response(200, json_body={"would_publish": True})

        monkeypatch.setattr("smartload_client.actions.httpx.post", fake_post)
        ac = ActionsClient(client)
        ac.simulate_isolate("backend_1")
        assert captured["json"]["status"] == "unhealthy"


class TestSmartLoadClientActionsWiring:

    def test_actions_subclient_is_attached(self):
        c = SmartLoadClient(base_url="http://example:8086")
        assert isinstance(c.actions, ActionsClient)
        c.close()

    def test_anomaly_detector_url_defaults_to_localhost(self):
        c = SmartLoadClient(base_url="http://example:8086")
        assert c.anomaly_detector_url == "http://localhost:8082"
        c.close()

    def test_anomaly_detector_url_picks_up_env_var(self, monkeypatch):
        monkeypatch.setenv("SMARTLOAD_ANOMALY_DETECTOR_URL", "http://env-anom:9090")
        c = SmartLoadClient(base_url="http://example:8086")
        assert c.anomaly_detector_url == "http://env-anom:9090"
        c.close()

    def test_anomaly_detector_url_strips_trailing_slash(self):
        c = SmartLoadClient(
            base_url="http://example:8086",
            anomaly_detector_url="http://my-anom:8082/",
        )
        assert c.anomaly_detector_url == "http://my-anom:8082"
        c.close()

    def test_top_level_scale_delegates(self, monkeypatch):
        c = SmartLoadClient(base_url="http://example:8086")
        captured = {}

        def fake_scale(target_count, *, actor=None, reason=None):
            captured["call"] = (target_count, actor, reason)
            return {"status": "applied"}

        monkeypatch.setattr(c.actions, "scale", fake_scale)
        c.scale(4, actor="alice", reason="drill")
        assert captured["call"] == (4, "alice", "drill")
        c.close()

    def test_top_level_isolate_delegates(self, monkeypatch):
        c = SmartLoadClient(base_url="http://example:8086")
        captured = {}

        def fake_isolate(backend_id, status, *, actor=None, reason=None):
            captured["call"] = (backend_id, status, actor, reason)
            return {"status": "applied"}

        monkeypatch.setattr(c.actions, "isolate", fake_isolate)
        c.isolate("backend_2", "degraded", actor="bob")
        assert captured["call"] == ("backend_2", "degraded", "bob", None)
        c.close()

    def test_top_level_simulate_scale_delegates(self, monkeypatch):
        c = SmartLoadClient(base_url="http://example:8086")
        captured = {}

        def fake_sim(target_count, *, actor=None, reason=None):
            captured["call"] = (target_count, actor, reason)
            return {"would_execute": True}

        monkeypatch.setattr(c.actions, "simulate_scale", fake_sim)
        c.simulate_scale(4, actor="alice", reason="drill")
        assert captured["call"] == (4, "alice", "drill")
        c.close()

    def test_top_level_simulate_isolate_delegates(self, monkeypatch):
        c = SmartLoadClient(base_url="http://example:8086")
        captured = {}

        def fake_sim(backend_id, status, *, actor=None, reason=None):
            captured["call"] = (backend_id, status, actor, reason)
            return {"would_publish": True}

        monkeypatch.setattr(c.actions, "simulate_isolate", fake_sim)
        c.simulate_isolate("backend_2", "degraded", actor="bob")
        assert captured["call"] == ("backend_2", "degraded", "bob", None)
        c.close()


# ── named-strategy surface (#150) ─────────────────────────────────────────────

from smartload_client.policy import PolicyClient  # noqa: E402


class TestPolicySetStrategy:

    def test_set_strategy_posts_to_strategy_endpoint(self):
        client = MagicMock()
        client.default_actor = "smartload-client"
        client._http.post.return_value = _fake_response(
            200,
            json_body={
                "status": "updated",
                "strategy": "ai-hybrid",
                "recommended_rl_mode": "active",
            },
        )
        pc = PolicyClient(client)
        result = pc.set_strategy("ai-hybrid", actor="alice")
        assert result["strategy"] == "ai-hybrid"
        assert result["recommended_rl_mode"] == "active"
        client._http.post.assert_called_once_with(
            "/api/v1/policy/strategy",
            json={"name": "ai-hybrid"},
            headers={"X-Actor": "alice"},
        )

    def test_set_strategy_defaults_actor_to_client_default(self):
        client = MagicMock()
        client.default_actor = "my-tool"
        client._http.post.return_value = _fake_response(
            200, json_body={"status": "updated"},
        )
        pc = PolicyClient(client)
        pc.set_strategy("round-robin")
        _args, kwargs = client._http.post.call_args
        assert kwargs["headers"] == {"X-Actor": "my-tool"}

    def test_set_strategy_unknown_name_raises_validation_error(self):
        client = MagicMock()
        client.default_actor = "smartload-client"
        client._http.post.return_value = _fake_response(
            400,
            json_body={
                "error": "unknown strategy 'bogus'; allowed: [...]",
                "field": "name",
                "allowed_strategies": ["ai-hybrid", "round-robin"],
            },
        )
        pc = PolicyClient(client)
        with pytest.raises(ValidationError) as exc:
            pc.set_strategy("bogus")
        assert exc.value.field == "name"


class TestSmartLoadClientStrategyWiring:

    def test_top_level_set_strategy_delegates(self, monkeypatch):
        c = SmartLoadClient(base_url="http://example:8086")
        captured = {}

        def fake_set_strategy(name, *, actor=None):
            captured["call"] = (name, actor)
            return {"status": "updated", "strategy": name}

        monkeypatch.setattr(c.policy, "set_strategy", fake_set_strategy)
        c.set_strategy("latency-aware", actor="ops")
        assert captured["call"] == ("latency-aware", "ops")
        c.close()


# ── status surface (slice #149 / OUI.9) ─────────────────────────────────────

from smartload_client.status import (  # noqa: E402
    ActivePolicySnapshot,
    RecentEvents,
    ServiceStatus,
    StatusResponse,
)


class TestStatusResponseFromDict:
    """Round-trip parsing of the BFF's /api/v1/status response."""

    def _full(self) -> dict:
        return {
            "generated_at": "2026-06-03T12:00:00Z",
            "overall": "ok",
            "services": {
                "policy-manager": {
                    "status": "ok",
                    "redis": True,
                    "timescaledb": True,
                    "policy_version": 31,
                },
                "rl-engine": {
                    "status": "ok",
                    "runloop_enabled": True,
                    "policy": "ppo",
                    "mode": "shadow",
                },
            },
            "active_policy": {
                "operating_mode": "hybrid",
                "safe_mode": False,
                "slo_p95_latency_ms": 200,
                "policy_version": 31,
            },
            "recent": {
                "last_policy_change": {"actor": "ops", "field": "safe_mode", "at": "2026-06-03"},
                "last_scaling_event": None,
            },
        }

    def test_full_payload_round_trips(self):
        parsed = StatusResponse.from_dict(self._full())
        assert parsed.overall == "ok"
        assert parsed.generated_at == "2026-06-03T12:00:00Z"
        assert isinstance(parsed.services["policy-manager"], ServiceStatus)
        # Status is split from the rest of the body, which goes into `extra`.
        pm = parsed.services["policy-manager"]
        assert pm.status == "ok"
        assert pm.extra["redis"] is True
        assert pm.extra["policy_version"] == 31
        # active_policy → typed snapshot
        assert isinstance(parsed.active_policy, ActivePolicySnapshot)
        assert parsed.active_policy.operating_mode == "hybrid"
        assert parsed.active_policy.safe_mode is False
        # recent → typed wrapper
        assert isinstance(parsed.recent, RecentEvents)
        assert parsed.recent.last_policy_change["actor"] == "ops"
        assert parsed.recent.last_scaling_event is None

    def test_missing_active_policy_yields_none(self):
        payload = self._full()
        payload["active_policy"] = None
        parsed = StatusResponse.from_dict(payload)
        assert parsed.active_policy is None

    def test_missing_recent_yields_empty_wrapper(self):
        payload = self._full()
        del payload["recent"]
        parsed = StatusResponse.from_dict(payload)
        assert parsed.recent.last_policy_change is None
        assert parsed.recent.last_scaling_event is None

    def test_missing_services_yields_empty_map(self):
        parsed = StatusResponse.from_dict({"generated_at": "X", "overall": "ok"})
        assert parsed.services == {}

    def test_to_dict_round_trip_preserves_status_pill(self):
        # The wire form keeps `status` inline alongside the extras — the
        # SDK splits it into the `status` field + `extra` dict, and
        # to_dict() must reassemble.
        original = self._full()
        round_tripped = StatusResponse.from_dict(original).to_dict()
        # The status survives the split/recombine.
        for name, svc in original["services"].items():
            assert round_tripped["services"][name]["status"] == svc["status"]
            # And every extra field survives.
            for k, v in svc.items():
                assert round_tripped["services"][name][k] == v


class TestStatusClient:
    """Behaviour of the StatusClient sub-client (no live network)."""

    def test_get_status_top_level_delegates(self, monkeypatch):
        c = SmartLoadClient(base_url="http://example:8086")
        sentinel = StatusResponse(
            generated_at="X", overall="ok", services={}
        )

        def fake_get():
            return sentinel

        monkeypatch.setattr(c.status, "get", fake_get)
        result = c.get_status()
        assert result is sentinel
        c.close()

    def test_get_uses_operator_ui_url(self, monkeypatch):
        c = SmartLoadClient(
            base_url="http://example:8086",
            operator_ui_url="http://my-bff:9090",
        )
        captured = {}

        class _Resp:
            status_code = 200
            def json(self):
                return {"generated_at": "X", "overall": "ok", "services": {}}

        class _Client:
            def __init__(self, *args, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def get(self, url):
                captured["url"] = url
                return _Resp()

        import smartload_client.status as status_mod
        monkeypatch.setattr(status_mod.httpx, "Client", _Client)
        c.status.get()
        assert captured["url"] == "http://my-bff:9090/api/v1/status"
        c.close()

    def test_get_raises_on_non_200(self, monkeypatch):
        c = SmartLoadClient(base_url="http://example:8086")

        class _Resp:
            status_code = 503
            def json(self):
                return {}

        class _Client:
            def __init__(self, *a, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url): return _Resp()

        import smartload_client.status as status_mod
        monkeypatch.setattr(status_mod.httpx, "Client", _Client)
        with pytest.raises(SmartLoadError):
            c.status.get()
        c.close()

    def test_get_raises_on_non_json_body(self, monkeypatch):
        c = SmartLoadClient(base_url="http://example:8086")

        class _Resp:
            status_code = 200
            def json(self):
                raise ValueError("not json")

        class _Client:
            def __init__(self, *a, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url): return _Resp()

        import smartload_client.status as status_mod
        monkeypatch.setattr(status_mod.httpx, "Client", _Client)
        with pytest.raises(SmartLoadError):
            c.status.get()
        c.close()
