"""
tests/e2e/policy-management/test_policy_walk.py
────────────────────────────────────────────────
End-to-end suite for the policy-management vertical slice. Uses the
SmartLoad SDK exclusively (no raw HTTP / Redis) — proves the SDK is the
real customer surface.

Requires a live docker-compose stack:
    docker compose up -d
    pytest tests/e2e/policy-management/ -v
"""

from __future__ import annotations

import threading
import time

import pytest

from smartload_client import SmartLoadClient, ValidationError

pytestmark = pytest.mark.e2e


# ── helpers ────────────────────────────────────────────────────────────────

def _next_distinct_max(current: int) -> int:
    """Return a max_backends value different from the current one, within range."""
    return (int(current) % 7) + 2


# ── read ───────────────────────────────────────────────────────────────────

class TestPolicyRead:

    def test_get_returns_canonical_fields(self, client: SmartLoadClient, policy_restore):
        p = client.get_policy()
        for k in (
            "operating_mode",
            "safe_mode",
            "min_backends",
            "max_backends",
            "policy_version",
        ):
            assert k in p, f"missing canonical field: {k}"

    def test_get_is_idempotent(self, client: SmartLoadClient, policy_restore):
        a = client.get_policy()
        b = client.get_policy()
        assert a == b


# ── write ──────────────────────────────────────────────────────────────────

class TestPolicyWrite:

    def test_update_returns_changed_fields(self, client: SmartLoadClient, policy_restore):
        new_max = _next_distinct_max(policy_restore["max_backends"])
        r = client.set_policy({"max_backends": new_max}, actor="e2e-suite")
        assert r["status"] == "updated"
        assert r["changed_fields"] == ["max_backends"]
        assert r["policy"]["max_backends"] == new_max
        assert r["policy_version"] >= 1

    def test_idempotent_repeat_is_noop(self, client: SmartLoadClient, policy_restore):
        new_max = _next_distinct_max(policy_restore["max_backends"])
        client.set_policy({"max_backends": new_max}, actor="e2e-suite")
        time.sleep(0.3)
        again = client.set_policy({"max_backends": new_max}, actor="e2e-suite")
        assert again["status"] == "no-op"
        assert again["changed_fields"] == []

    def test_invalid_raises_validation_error(self, client: SmartLoadClient, policy_restore):
        # min > max — fails the cross-field invariant.
        with pytest.raises(ValidationError) as exc:
            client.set_policy({"max_backends": 1, "min_backends": 99})
        assert exc.value.field in ("min_backends", "max_backends")

    def test_unknown_operating_mode_raises_validation_error(
        self, client: SmartLoadClient, policy_restore,
    ):
        with pytest.raises(ValidationError) as exc:
            client.set_policy({"operating_mode": "rogue"})
        assert exc.value.field == "operating_mode"


# ── subscribe ──────────────────────────────────────────────────────────────

class TestPolicySubscribe:

    def test_envelope_arrives_within_5s(self, client: SmartLoadClient, policy_restore):
        received: list[dict] = []
        evt = threading.Event()

        def on_update(payload, _meta):
            received.append(payload)
            evt.set()

        sub = client.subscribe_policy(on_update)
        try:
            # Drain any backlog from cooperating tests.
            time.sleep(0.3)
            evt.clear()
            received.clear()

            new_max = _next_distinct_max(policy_restore["max_backends"])
            client.set_policy({"max_backends": new_max}, actor="e2e-subscribe")
            assert evt.wait(timeout=5.0), "no smartload.policy envelope arrived within 5s"
            assert received[-1]["max_backends"] == new_max
        finally:
            sub.close()

    def test_callback_exception_does_not_kill_thread(
        self, client: SmartLoadClient, policy_restore,
    ):
        """A buggy callback must not silently kill the subscription."""
        received: list[dict] = []
        evt = threading.Event()
        flake_call_count = [0]

        def on_update(payload, _meta):
            flake_call_count[0] += 1
            if flake_call_count[0] == 1:
                raise RuntimeError("simulated buggy callback")
            received.append(payload)
            evt.set()

        sub = client.subscribe_policy(on_update)
        try:
            time.sleep(0.3)
            evt.clear()
            received.clear()
            flake_call_count[0] = 0

            # Two changes → first callback raises, second must succeed.
            new_max_1 = _next_distinct_max(policy_restore["max_backends"])
            client.set_policy({"max_backends": new_max_1}, actor="e2e-flake-1")
            time.sleep(0.3)
            new_max_2 = _next_distinct_max(new_max_1)
            client.set_policy({"max_backends": new_max_2}, actor="e2e-flake-2")
            assert evt.wait(timeout=5.0), "second envelope did not arrive — thread died"
        finally:
            sub.close()


# ── audit ──────────────────────────────────────────────────────────────────

class TestPolicyAudit:

    def test_audit_returns_recent_change(self, client: SmartLoadClient, policy_restore):
        new_max = _next_distinct_max(policy_restore["max_backends"])
        client.set_policy({"max_backends": new_max}, actor="e2e-audit")
        time.sleep(0.5)  # audit write is best-effort but fast
        rows = client.audit_policy(limit=20)
        assert isinstance(rows, list)
        matching = [
            r for r in rows
            if r.get("field") == "max_backends"
            and r.get("new_value") == new_max
            and r.get("actor") == "e2e-audit"
        ]
        assert matching, f"audit row not found among latest {len(rows)} rows"
        row = matching[0]
        for k in ("time", "policy_version", "field", "old_value", "new_value", "actor"):
            assert k in row

    def test_audit_limit_caps_results(self, client: SmartLoadClient, policy_restore):
        rows = client.audit_policy(limit=1)
        assert isinstance(rows, list)
        assert len(rows) <= 1
