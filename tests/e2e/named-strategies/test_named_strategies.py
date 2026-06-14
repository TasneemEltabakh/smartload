"""
tests/e2e/named-strategies/test_named_strategies.py
─────────────────────────────────────────────────────
End-to-end suite for the named-strategies slice (#150). Uses the SDK against
policy-manager (port 8086).

Requires a live docker-compose stack:
    docker compose up -d
    pytest tests/e2e/named-strategies/ -v
"""

from __future__ import annotations

import time

import httpx
import pytest

from smartload_client import SmartLoadClient, ValidationError

pytestmark = pytest.mark.e2e


# The strategies whose name survives the GET round-trip (the representative names
# the reverse map returns). Non-representative names (least-connections,
# forecast-aware, anomaly-aware, ai-hybrid) collapse to their representative on
# read — that is the documented many-to-one behaviour, exercised separately.
_REPRESENTATIVE = ["round-robin", "latency-aware", "safe-fallback"]

_RL_EXPECTED = {
    "round-robin": None,
    "least-connections": None,
    "latency-aware": "shadow",
    "forecast-aware": "shadow",
    "anomaly-aware": "shadow",
    "ai-hybrid": "active",
    "safe-fallback": None,
}


# ── set + derived read ───────────────────────────────────────────────────────

class TestSetStrategy:

    def test_set_known_strategy_applies_primitives(
        self, client: SmartLoadClient, policy_restore,
    ):
        r = client.set_strategy("ai-hybrid", actor="e2e-strategy")
        assert r["status"] in ("updated", "no-op")
        assert r["strategy"] == "ai-hybrid"
        assert r["recommended_rl_mode"] == "active"
        assert r["policy"]["operating_mode"] == "hybrid"
        assert r["policy"]["safe_mode"] is False
        # rl_mode is a deploy-time pin, never a policy field.
        assert "rl_mode" not in r["policy"]

    def test_set_safe_fallback_flips_safe_mode(
        self, client: SmartLoadClient, policy_restore,
    ):
        r = client.set_strategy("safe-fallback", actor="e2e-strategy")
        assert r["policy"]["operating_mode"] == "classical-only"
        assert r["policy"]["safe_mode"] is True
        assert r["recommended_rl_mode"] is None

    def test_unknown_strategy_raises_validation_error(
        self, client: SmartLoadClient, policy_restore,
    ):
        with pytest.raises(ValidationError) as exc:
            client.set_strategy("round_robin")  # underscore, not canonical
        assert exc.value.field == "name"

    def test_unknown_strategy_400_lists_allowed(
        self, client: SmartLoadClient, policy_url, policy_restore,
    ):
        """Hit the endpoint directly to inspect the allowed_strategies list the
        SDK does not surface on the exception."""
        r = httpx.post(
            f"{policy_url}/api/v1/policy/strategy",
            json={"name": "bogus"},
            timeout=5.0,
        )
        assert r.status_code == 400
        body = r.json()
        assert body["field"] == "name"
        assert "ai-hybrid" in body["allowed_strategies"]
        assert "safe-fallback" in body["allowed_strategies"]


# ── roundtrip property: representative strategies survive GET ─────────────────

class TestStrategyRoundtrip:

    @pytest.mark.parametrize("name", _REPRESENTATIVE)
    def test_set_then_get_returns_same_strategy_name(
        self, client: SmartLoadClient, name, policy_restore,
    ):
        client.set_strategy(name, actor="e2e-roundtrip")
        policy = client.get_policy()
        assert policy["strategy_name"] == name

    @pytest.mark.parametrize(
        "name", ["least-connections", "forecast-aware", "anomaly-aware", "ai-hybrid"],
    )
    def test_non_representative_collapses_to_representative(
        self, client: SmartLoadClient, name, policy_restore,
    ):
        """Documented many-to-one: non-representative strategies reverse-map to
        the representative for their primitive pair, never back to themselves."""
        client.set_strategy(name, actor="e2e-roundtrip")
        policy = client.get_policy()
        assert policy["strategy_name"] != name
        assert policy["strategy_name"] in ("round-robin", "latency-aware", "safe-fallback")

    @pytest.mark.parametrize("name", sorted(_RL_EXPECTED))
    def test_recommended_rl_mode_matches_table(
        self, client: SmartLoadClient, name, policy_restore,
    ):
        r = client.set_strategy(name, actor="e2e-rl")
        assert r["recommended_rl_mode"] == _RL_EXPECTED[name]


# ── custom: direct primitives that match no strategy ─────────────────────────

class TestCustomStrategy:

    def test_direct_primitives_no_match_yields_custom(
        self, client: SmartLoadClient, policy_restore,
    ):
        """Setting primitives directly that match no documented strategy
        (rl-only) ⟹ GET returns strategy_name == 'custom'."""
        client.set_policy({"operating_mode": "rl-only", "safe_mode": False},
                          actor="e2e-custom")
        policy = client.get_policy()
        assert policy["strategy_name"] == "custom"


# ── audit round-trip ─────────────────────────────────────────────────────────

class TestStrategyAudit:

    def test_strategy_change_records_audit_with_strategy_in_actor(
        self, client: SmartLoadClient, policy_restore,
    ):
        """The audit row records the strategy name in the actor field
        (`strategy:<name>:<actor>`) so the change is grep-able by intent."""
        # Force a real change: start from a known different state.
        client.set_strategy("safe-fallback", actor="e2e-audit-setup")
        client.set_strategy("ai-hybrid", actor="e2e-audit")

        deadline = time.monotonic() + 5.0
        matched = None
        while time.monotonic() < deadline:
            rows = client.audit_policy(limit=20)
            for row in rows:
                if str(row.get("actor", "")).startswith("strategy:ai-hybrid:"):
                    matched = row
                    break
            if matched:
                break
            time.sleep(0.2)

        assert matched is not None, (
            "expected policy_changes row with actor prefixed "
            "'strategy:ai-hybrid:' within 5s"
        )
        assert matched["actor"] == "strategy:ai-hybrid:e2e-audit"
