"""
tests/unit/policy-manager/test_strategies.py
─────────────────────────────────────────────
Pure-Python unit tests for services/policy-manager/strategies.py (#150).

No Docker, no Redis, no DB — runs in the unit-tests CI job.

Coverage:
  1. Forward translation: every documented strategy → its primitives, with
     the policy-field subset (operating_mode + safe_mode) and the recommended
     (never-applied) RL_MODE pin.
  2. Validation: unknown / empty / non-string names raise StrategyError with
     the allowed list attached.
  3. Reverse map: representative-name choice for each primitive pair, "custom"
     for unmatched pairs, and the documented many-to-one ambiguity.
  4. Cross-module contract: every strategy's policy-field output passes
     policy-manager's real validate_merged_policy (the operating_mode enum gate)
     — guards against the classical vs classical-only drift class.
  5. Reverse-then-forward round-trip is consistent for the representative names.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "policy-manager"
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from strategies import (  # noqa: E402
    ALLOWED_STRATEGIES,
    CUSTOM_STRATEGY,
    STRATEGIES,
    StrategyError,
    name_to_policy,
    name_to_primitives,
    primitives_to_name,
    recommended_rl_mode,
)
from validation import (  # noqa: E402
    VALID_OPERATING_MODES,
    validate_merged_policy,
)


# The seven canonical strategies #150 documents.
_EXPECTED_STRATEGIES = {
    "round-robin",
    "least-connections",
    "latency-aware",
    "forecast-aware",
    "anomaly-aware",
    "ai-hybrid",
    "safe-fallback",
}


# ── table shape ──────────────────────────────────────────────────────────────

class TestTable:
    def test_allowed_strategies_match_the_documented_set(self):
        assert set(ALLOWED_STRATEGIES) == _EXPECTED_STRATEGIES

    def test_allowed_strategies_is_sorted_and_stable(self):
        assert list(ALLOWED_STRATEGIES) == sorted(ALLOWED_STRATEGIES)

    def test_safe_fallback_is_the_only_safe_mode_strategy(self):
        safe = [n for n, s in STRATEGIES.items() if s["safe_mode"]]
        assert safe == ["safe-fallback"]


# ── forward translation ──────────────────────────────────────────────────────

class TestForward:
    @pytest.mark.parametrize(
        "name, operating_mode, safe_mode, rl_mode",
        [
            ("round-robin", "classical-only", False, None),
            ("least-connections", "classical-only", False, None),
            ("latency-aware", "hybrid", False, "shadow"),
            ("forecast-aware", "hybrid", False, "shadow"),
            ("anomaly-aware", "hybrid", False, "shadow"),
            ("ai-hybrid", "hybrid", False, "active"),
            ("safe-fallback", "classical-only", True, None),
        ],
    )
    def test_name_to_primitives(self, name, operating_mode, safe_mode, rl_mode):
        prims = name_to_primitives(name)
        assert prims == {
            "operating_mode": operating_mode,
            "safe_mode": safe_mode,
            "rl_mode": rl_mode,
        }

    def test_name_to_policy_omits_rl_mode(self):
        # The policy-field subset must NEVER carry rl_mode (deploy-time pin).
        for name in ALLOWED_STRATEGIES:
            policy = name_to_policy(name)
            assert set(policy) == {"operating_mode", "safe_mode"}
            assert "rl_mode" not in policy

    def test_recommended_rl_mode(self):
        assert recommended_rl_mode("ai-hybrid") == "active"
        assert recommended_rl_mode("latency-aware") == "shadow"
        assert recommended_rl_mode("round-robin") is None

    def test_name_to_primitives_returns_fresh_dict(self):
        # Mutating the returned dict must not corrupt the shared table.
        a = name_to_primitives("ai-hybrid")
        a["operating_mode"] = "tampered"
        b = name_to_primitives("ai-hybrid")
        assert b["operating_mode"] == "hybrid"


# ── validation ───────────────────────────────────────────────────────────────

class TestValidation:
    def test_unknown_name_raises_with_allowed_list(self):
        with pytest.raises(StrategyError) as exc:
            name_to_policy("round_robin")  # underscore — not the canonical name
        assert exc.value.allowed == ALLOWED_STRATEGIES
        assert "round_robin" in str(exc.value)

    @pytest.mark.parametrize("bad", [None, "", 42, [], {}, True])
    def test_empty_or_non_string_name_raises(self, bad):
        with pytest.raises(StrategyError) as exc:
            name_to_primitives(bad)
        assert exc.value.allowed == ALLOWED_STRATEGIES

    def test_error_message_lists_every_allowed_strategy(self):
        with pytest.raises(StrategyError) as exc:
            name_to_policy("bogus")
        msg = str(exc.value)
        for name in ALLOWED_STRATEGIES:
            assert name in msg


# ── reverse map ──────────────────────────────────────────────────────────────

class TestReverse:
    def test_classical_only_false_maps_to_round_robin(self):
        assert primitives_to_name("classical-only", False) == "round-robin"

    def test_classical_only_true_maps_to_safe_fallback(self):
        assert primitives_to_name("classical-only", True) == "safe-fallback"

    def test_hybrid_false_maps_to_latency_aware(self):
        # Representative choice for the (hybrid, False) pair shared by
        # latency/forecast/anomaly-aware + ai-hybrid.
        assert primitives_to_name("hybrid", False) == "latency-aware"

    @pytest.mark.parametrize(
        "operating_mode, safe_mode",
        [
            ("rl-only", False),       # valid enum, no strategy uses it
            ("rl-only", True),
            ("hybrid", True),         # hybrid + kill switch — not a documented combo
            ("classical-only", "nonbool"),  # malformed safe_mode → treated False, still known
        ],
    )
    def test_unmatched_pairs_map_to_custom_or_known(self, operating_mode, safe_mode):
        result = primitives_to_name(operating_mode, safe_mode)
        # rl-only and hybrid+True are genuinely unmatched → custom.
        if operating_mode == "rl-only" or (operating_mode == "hybrid" and safe_mode is True):
            assert result == CUSTOM_STRATEGY
        else:
            # classical-only + non-bool safe_mode coerces to False → round-robin.
            assert result == "round-robin"

    def test_unknown_operating_mode_maps_to_custom(self):
        assert primitives_to_name("totally-unknown", False) == CUSTOM_STRATEGY

    def test_nonbool_safe_mode_is_coerced_to_false(self):
        # Defensive against malformed on-disk policy.
        assert primitives_to_name("classical-only", None) == "round-robin"
        assert primitives_to_name("classical-only", "true") == "round-robin"


# ── cross-module contract: every strategy yields a valid policy ──────────────

class TestPolicyValidatorContract:
    """The strategy table's operating_mode values MUST be the canonical enum
    policy-manager's validator accepts (classical-only / hybrid / rl-only), not
    the loose `classical` shorthand. Render every strategy through the real
    validator so the two can't drift."""

    @pytest.mark.parametrize("name", sorted(_EXPECTED_STRATEGIES))
    def test_strategy_policy_passes_real_validator(self, name):
        policy = name_to_policy(name)
        assert policy["operating_mode"] in VALID_OPERATING_MODES
        # Build a complete merged policy and run the real cross-field validator.
        merged = {
            "operating_mode": "hybrid",
            "safe_mode": False,
            "min_backends": 1,
            "max_backends": 10,
            "slo_p95_latency_ms": 200,
            "anomaly_latency_multiplier": 3,
            "per_instance_capacity_rps": 100,
            "autoscaler_cooldown_seconds": 60,
        }
        merged.update(policy)
        validate_merged_policy(merged)  # raises on failure


# ── round-trip consistency for representative names ──────────────────────────

class TestRoundTrip:
    @pytest.mark.parametrize("name", ["round-robin", "safe-fallback", "latency-aware"])
    def test_representative_names_round_trip(self, name):
        policy = name_to_policy(name)
        derived = primitives_to_name(policy["operating_mode"], policy["safe_mode"])
        assert derived == name

    @pytest.mark.parametrize(
        "name", ["least-connections", "forecast-aware", "anomaly-aware", "ai-hybrid"],
    )
    def test_non_representative_names_collapse_to_their_representative(self, name):
        """Non-representative strategies reverse-map to the representative for
        their primitive pair, never back to themselves — the documented
        many-to-one behaviour."""
        policy = name_to_policy(name)
        derived = primitives_to_name(policy["operating_mode"], policy["safe_mode"])
        assert derived != name
        # All four share a known primitive pair, so the derived value is a real
        # representative, never "custom".
        assert derived in ALLOWED_STRATEGIES
