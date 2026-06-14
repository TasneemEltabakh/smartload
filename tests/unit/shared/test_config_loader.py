"""
tests/unit/shared/test_config_loader.py
────────────────────────────────────────
Unit tests for the single-file client bootstrap (services/shared/config_loader.py).

Hermetic: the pure validate / to_policy / to_env / merge_policy functions take
plain dicts, so nothing here needs PyYAML, redis or the filesystem. read_file
(the one yaml-touching helper) is exercised via tmp_path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVICES = Path(__file__).resolve().parents[3] / "services"
if str(_SERVICES) not in sys.path:
    sys.path.insert(0, str(_SERVICES))

from shared.config_loader import (  # noqa: E402
    STRATEGY_PRIMITIVES,
    SmartLoadConfigError,
    merge_policy,
    read_file,
    to_env,
    to_policy,
    validate,
)


def _minimal():
    """The smallest valid bootstrap mapping: just the required strategy.name."""
    return {"strategy": {"name": "latency-aware"}}


def _full():
    return {
        "metrics": {"provider": "prometheus", "url": "http://p:9090"},
        "loadBalancer": {"type": "nginx", "apiUrl": "http://lb:8080"},
        "orchestrator": {"type": "docker"},
        "service": {"name": "payment", "namespace": "prod"},
        "slo": {"p95LatencyMs": 250, "errorRate": 0.005},
        "strategy": {"name": "ai-hybrid", "evaluationIntervalSeconds": 15, "cooldownSeconds": 60},
        "backends": [{"name": "payment-v1", "prometheusJob": "payment-v1"}],
    }


# ── validate: happy paths ───────────────────────────────────────────────────────

def test_validate_minimal_ok():
    validate(_minimal())  # no raise


def test_validate_full_ok():
    validate(_full())  # no raise


@pytest.mark.parametrize("name", sorted(STRATEGY_PRIMITIVES))
def test_validate_every_known_strategy(name):
    validate({"strategy": {"name": name}})


# ── validate: field-named errors ────────────────────────────────────────────────

def test_validate_requires_strategy_name():
    with pytest.raises(SmartLoadConfigError, match="strategy.name is required"):
        validate({"slo": {"p95LatencyMs": 100}})


def test_validate_rejects_unknown_strategy():
    with pytest.raises(SmartLoadConfigError, match="strategy.name"):
        validate({"strategy": {"name": "magic-router"}})


def test_validate_unknown_strategy_lists_allowed():
    with pytest.raises(SmartLoadConfigError, match="round-robin"):
        validate({"strategy": {"name": "nope"}})


def test_validate_rejects_negative_latency():
    with pytest.raises(SmartLoadConfigError, match="slo.p95LatencyMs"):
        validate({"strategy": {"name": "latency-aware"}, "slo": {"p95LatencyMs": -5}})


def test_validate_rejects_error_rate_out_of_range():
    with pytest.raises(SmartLoadConfigError, match="slo.errorRate"):
        validate({"strategy": {"name": "latency-aware"}, "slo": {"errorRate": 1.5}})


def test_validate_rejects_bool_as_number():
    # bool is an int subclass — guard against True sneaking in as p95LatencyMs.
    with pytest.raises(SmartLoadConfigError, match="slo.p95LatencyMs"):
        validate({"strategy": {"name": "latency-aware"}, "slo": {"p95LatencyMs": True}})


def test_validate_rejects_fractional_cooldown():
    with pytest.raises(SmartLoadConfigError, match="strategy.cooldownSeconds"):
        validate({"strategy": {"name": "latency-aware", "cooldownSeconds": 1.5}})


def test_validate_rejects_unknown_lb_type():
    with pytest.raises(SmartLoadConfigError, match="loadBalancer.type"):
        validate({"strategy": {"name": "latency-aware"}, "loadBalancer": {"type": "f5"}})


def test_validate_rejects_unknown_orchestrator():
    with pytest.raises(SmartLoadConfigError, match="orchestrator.type"):
        validate({"strategy": {"name": "latency-aware"}, "orchestrator": {"type": "nomad"}})


def test_validate_rejects_unknown_metrics_provider():
    with pytest.raises(SmartLoadConfigError, match="metrics.provider"):
        validate({"strategy": {"name": "latency-aware"}, "metrics": {"provider": "datadog"}})


def test_validate_rejects_non_list_backends():
    with pytest.raises(SmartLoadConfigError, match="backends must be a list"):
        validate({"strategy": {"name": "latency-aware"}, "backends": {"name": "x"}})


def test_validate_requires_backend_name():
    with pytest.raises(SmartLoadConfigError, match=r"backends\[0\].name"):
        validate({"strategy": {"name": "latency-aware"}, "backends": [{"prometheusJob": "x"}]})


def test_validate_rejects_non_mapping_section():
    with pytest.raises(SmartLoadConfigError, match="strategy must be a mapping"):
        validate({"strategy": "latency-aware"})


# ── to_policy ───────────────────────────────────────────────────────────────────

def test_to_policy_maps_strategy_to_primitives():
    p = to_policy({"strategy": {"name": "ai-hybrid"}})
    assert p["operating_mode"] == "hybrid"
    assert p["safe_mode"] is False


def test_to_policy_classical_strategy():
    # The canonical policy.yaml enum is "classical-only", not the loose
    # "classical" shorthand in the #150 table — see the validator regression test.
    p = to_policy({"strategy": {"name": "round-robin"}})
    assert p["operating_mode"] == "classical-only"


def test_to_policy_maps_slo_and_cooldown():
    p = to_policy(_full())
    assert p["slo_p95_latency_ms"] == 250
    assert p["autoscaler_cooldown_seconds"] == 60


def test_to_policy_omits_absent_optional_fields():
    p = to_policy(_minimal())
    assert "slo_p95_latency_ms" not in p
    assert "autoscaler_cooldown_seconds" not in p


def test_to_policy_never_sets_version():
    assert "policy_version" not in to_policy(_full())


# ── to_env ──────────────────────────────────────────────────────────────────────

def test_to_env_eval_interval_and_rl_mode():
    env = to_env(_full())
    assert env["POLL_INTERVAL_SECONDS"] == "15"
    assert env["RL_MODE"] == "active"  # ai-hybrid


def test_to_env_shadow_for_hybrid():
    assert to_env({"strategy": {"name": "latency-aware"}})["RL_MODE"] == "shadow"


def test_to_env_omits_rl_mode_for_classical():
    # round-robin does not engage the RL plane — leave RL_MODE at its default.
    assert "RL_MODE" not in to_env({"strategy": {"name": "round-robin"}})


def test_to_env_minimal_has_only_rl_mode():
    # latency-aware implies RL shadow; no eval interval -> no POLL_INTERVAL_SECONDS.
    assert to_env(_minimal()) == {"RL_MODE": "shadow"}


def test_to_env_classical_with_no_interval_is_empty():
    assert to_env({"strategy": {"name": "round-robin"}}) == {}


# ── merge_policy ────────────────────────────────────────────────────────────────

def test_merge_preserves_existing_version():
    existing = {"policy_version": 35, "min_backends": 2, "max_backends": 8}
    merged = merge_policy(existing, _full())
    assert merged["policy_version"] == 35          # not rolled back
    assert merged["min_backends"] == 2             # untouched passthrough
    assert merged["operating_mode"] == "hybrid"    # overlaid from smartload.yml


def test_merge_defaults_version_when_no_existing():
    merged = merge_policy(None, _minimal())
    assert merged["policy_version"] == 1


def test_merge_overwrites_driven_fields():
    existing = {"operating_mode": "classical", "safe_mode": True}
    merged = merge_policy(existing, {"strategy": {"name": "ai-hybrid"}})
    assert merged["operating_mode"] == "hybrid"
    assert merged["safe_mode"] is False


# ── read_file ───────────────────────────────────────────────────────────────────

def test_read_file_absent_returns_none(tmp_path):
    assert read_file(str(tmp_path / "nope.yml")) is None


def test_read_file_empty_returns_empty_dict(tmp_path):
    f = tmp_path / "smartload.yml"
    f.write_text("", encoding="utf-8")
    assert read_file(str(f)) == {}


def test_read_file_parses_and_round_trips(tmp_path):
    f = tmp_path / "smartload.yml"
    f.write_text("strategy:\n  name: latency-aware\n", encoding="utf-8")
    raw = read_file(str(f))
    assert raw == {"strategy": {"name": "latency-aware"}}
    validate(raw)  # parsed shape is valid


def test_read_file_rejects_non_mapping_root(tmp_path):
    f = tmp_path / "smartload.yml"
    f.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(SmartLoadConfigError, match="<root>"):
        read_file(str(f))


# ── cross-module contract: rendered policy must satisfy the canonical validator ──
#
# policy-manager is the sole writer + validator of policy.yaml, so anything the
# bootstrap renders must pass validate_merged_policy. This is the test that
# caught operating_mode "classical" (rejected) vs the canonical "classical-only".

def _load_policy_validator():
    import importlib.util

    path = (Path(__file__).resolve().parents[3]
            / "services" / "policy-manager" / "validation.py")
    spec = importlib.util.spec_from_file_location("pm_validation", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # @dataclass resolves cls.__module__ via sys.modules
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", sorted(STRATEGY_PRIMITIVES))
def test_rendered_policy_passes_policy_manager_validator(name):
    pm = _load_policy_validator()
    # Merge onto a realistic existing policy (the canonical fields) the way the
    # bootstrap CLI does, then run the canonical gate.
    existing = {
        "policy_version": 35, "min_backends": 1, "max_backends": 10,
        "operating_mode": "hybrid", "safe_mode": False, "slo_p95_latency_ms": 200,
        "anomaly_response": "auto-isolate", "anomaly_recovery_window_seconds": 30,
        "autoscaler_cooldown_seconds": 60, "per_instance_capacity_rps": 100,
        "anomaly_latency_multiplier": 3, "rl_exploration_rate": 0,
        "rl_confidence_threshold": 0.6,
    }
    merged = merge_policy(existing, _full() | {"strategy": {"name": name,
                                                            "cooldownSeconds": 45}})
    pm.validate_merged_policy(merged)  # raises PolicyValidationError on mismatch


def test_rendered_operating_mode_is_in_validator_enum():
    pm = _load_policy_validator()
    rendered_modes = {p["operating_mode"] for p in
                      (to_policy({"strategy": {"name": n}}) for n in STRATEGY_PRIMITIVES)}
    assert rendered_modes <= set(pm.VALID_OPERATING_MODES)
