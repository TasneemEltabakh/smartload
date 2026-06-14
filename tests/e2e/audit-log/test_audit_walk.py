"""
tests/e2e/audit-log/test_audit_walk.py
───────────────────────────────────────
End-to-end suite for the audit-log vertical slice (#122). Uses the
SmartLoad SDK exclusively so the suite exercises the customer surface
end-to-end across both upstreams (policy-manager + autoscaler).

Requires a live docker-compose stack:
    docker compose up -d
    pytest tests/e2e/audit-log/ -v
"""

from __future__ import annotations

import time

import httpx
import pytest

from smartload_client import SmartLoadClient, ValidationError

pytestmark = pytest.mark.e2e


# ── shared helpers ────────────────────────────────────────────────────────────

def _next_distinct_max(current: int) -> int:
    """Return a max_backends value different from current and within range."""
    return (int(current) % 7) + 2


# ── per-kind read ─────────────────────────────────────────────────────────────

class TestAuditPolicyRead:

    def test_policy_audit_returns_list(self, client: SmartLoadClient, policy_restore):
        rows = client.list_audit("policy", limit=20)
        assert isinstance(rows, list)
        # Every shipped row must carry the canonical column shape.
        for row in rows:
            for k in ("time", "policy_version", "field", "old_value", "new_value", "actor"):
                assert k in row, f"policy audit row missing field: {k}"

    def test_policy_audit_via_audit_subclient(
        self, client: SmartLoadClient, policy_restore,
    ):
        """list_audit('policy') and audit.policy() must return the same rows."""
        a = client.list_audit("policy", limit=5)
        b = client.audit.policy(limit=5)
        assert a == b


class TestAuditScalingRead:

    def test_scaling_audit_returns_list(self, client: SmartLoadClient):
        rows = client.list_audit("scaling", limit=20)
        assert isinstance(rows, list)
        for row in rows:
            for k in ("time", "action", "instance_count"):
                assert k in row, f"scaling audit row missing field: {k}"
            # A manual scale (POST /api/v1/scale) writes one scaling_events
            # row per operator click; when no step actuates (target == current
            # or the cluster rejected every step) the row's action is "noop"
            # by design, so the audit stream can legitimately carry it.
            assert row["action"] in ("scale_out", "scale_in", "noop"), (
                f"unexpected action: {row['action']}"
            )

    def test_scaling_audit_targets_autoscaler_upstream(
        self, client: SmartLoadClient, autoscaler_url,
    ):
        """The SDK must hit autoscaler_url for scaling-audit, not base_url
        (policy-manager). A control-test that breaks if a future refactor
        misroutes the call."""
        # Hit the SDK
        sdk_rows = client.list_audit("scaling", limit=3)
        # Hit the autoscaler endpoint directly with httpx
        direct = httpx.get(
            f"{autoscaler_url}/api/v1/audit/scaling",
            params={"limit": 3},
            timeout=5.0,
        ).json()
        assert sdk_rows == direct


# ── limit cap + validation ────────────────────────────────────────────────────

class TestAuditLimits:

    def test_limit_caps_results(self, client: SmartLoadClient):
        for kind in ("policy", "scaling"):
            rows = client.list_audit(kind, limit=1)
            assert len(rows) <= 1, f"{kind} audit ignored limit=1"

    def test_default_limit_returns_at_most_50(self, client: SmartLoadClient):
        """The SDK default + the server cap together must yield ≤50 rows."""
        for kind in ("policy", "scaling"):
            rows = client.list_audit(kind)
            assert len(rows) <= 50, f"{kind} audit returned more than 50 default rows"

    def test_invalid_limit_returns_400(self, client: SmartLoadClient, policy_url):
        """The endpoint rejects non-integer limit with HTTP 400 + a field hint."""
        r = httpx.get(
            f"{policy_url}/api/v1/audit/policy",
            params={"limit": "abc"},
            timeout=5.0,
        )
        assert r.status_code == 400
        body = r.json()
        assert body.get("field") == "limit"

    def test_negative_limit_returns_400(self, client: SmartLoadClient, autoscaler_url):
        r = httpx.get(
            f"{autoscaler_url}/api/v1/audit/scaling",
            params={"limit": "-3"},
            timeout=5.0,
        )
        assert r.status_code == 400
        body = r.json()
        assert body.get("field") == "limit"


# ── dispatch ─────────────────────────────────────────────────────────────────

class TestAuditKindDispatch:

    def test_unknown_kind_raises_validation_error(self, client: SmartLoadClient):
        with pytest.raises(ValidationError) as exc:
            client.list_audit("not-a-kind")  # type: ignore[arg-type]
        assert exc.value.field == "kind"

    def test_policy_and_scaling_return_different_shapes(self, client: SmartLoadClient):
        """Smoke that we're actually hitting two distinct streams — the
        schemas differ enough that a misroute would surface here."""
        pol = client.list_audit("policy", limit=5)
        sca = client.list_audit("scaling", limit=5)
        # Both lists; if either has data, the shapes must differ.
        if pol and sca:
            assert set(pol[0].keys()) != set(sca[0].keys())
            assert "field" in pol[0] and "field" not in sca[0]
            assert "action" in sca[0] and "action" not in pol[0]


# ── write → audit round-trip ──────────────────────────────────────────────────

class TestAuditRoundTrip:

    def test_policy_change_appears_in_audit_within_5s(
        self, client: SmartLoadClient, policy_restore,
    ):
        """A successful policy POST must produce a matching policy-audit row
        visible via the SDK within a few seconds."""
        new_max = _next_distinct_max(policy_restore["max_backends"])
        client.set_policy({"max_backends": new_max}, actor="e2e-audit-roundtrip")

        deadline = time.monotonic() + 5.0
        matching: list[dict] = []
        while time.monotonic() < deadline and not matching:
            rows = client.list_audit("policy", limit=20)
            matching = [
                r for r in rows
                if r.get("field") == "max_backends"
                and r.get("new_value") == new_max
                and r.get("actor") == "e2e-audit-roundtrip"
            ]
            if matching:
                break
            time.sleep(0.2)

        assert matching, (
            "no matching audit row found within 5s after policy change "
            f"(latest {len(rows)} rows searched)"
        )
        row = matching[0]
        for k in ("time", "policy_version", "field", "old_value", "new_value", "actor"):
            assert k in row
