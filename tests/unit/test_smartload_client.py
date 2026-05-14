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
