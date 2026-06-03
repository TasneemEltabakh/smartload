"""
services/policy-manager/validation.py
──────────────────────────────────────
Pure-Python validation rules for policy.yaml fields. Imported by the
policy-manager service for POST handling and service-startup checks.

Kept as a standalone module (no Flask / Redis / TimescaleDB imports) so the
rules can be unit-tested without the Docker stack — same pattern as
services/autoscaler/decisions.py.

Validation contract (SOT §8.9):
  - Missing fields are allowed on POST (partial update) and fall back to
    defaults on startup. *Invalid* values are rejected in both cases.
  - The merged policy (existing ∪ updates) must satisfy every cross-field
    invariant (e.g. min_backends ≤ max_backends).
  - The error message tells the operator which field was wrong and why.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


VALID_OPERATING_MODES = ("classical-only", "hybrid", "rl-only")
VALID_ANOMALY_RESPONSES = ("auto-isolate", "advisory")


@dataclass
class PolicyValidationError(Exception):
    """Raised when a policy payload fails validation. The `field` attribute
    pinpoints the offending key so the HTTP layer can echo it back."""
    message: str
    field: str | None = None

    def __str__(self) -> str:
        return self.message


# ── per-field type / range checks ─────────────────────────────────────────────

def _require_bool(name: str, value: Any) -> None:
    if not isinstance(value, bool):
        raise PolicyValidationError(
            f"{name} must be a boolean (got {type(value).__name__})", field=name,
        )


def _require_positive_int(name: str, value: Any) -> None:
    # bool is a subclass of int in Python, so reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyValidationError(
            f"{name} must be an integer (got {type(value).__name__})", field=name,
        )
    if value <= 0:
        raise PolicyValidationError(
            f"{name} must be > 0 (got {value})", field=name,
        )


def _require_nonneg_number(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyValidationError(
            f"{name} must be a number (got {type(value).__name__})", field=name,
        )
    if value < 0:
        raise PolicyValidationError(
            f"{name} must be >= 0 (got {value})", field=name,
        )


def _require_unit_interval(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyValidationError(
            f"{name} must be a number (got {type(value).__name__})", field=name,
        )
    if not (0.0 <= value <= 1.0):
        raise PolicyValidationError(
            f"{name} must be in [0, 1] (got {value})", field=name,
        )


def _require_enum(name: str, value: Any, choices: tuple[str, ...]) -> None:
    if not isinstance(value, str):
        raise PolicyValidationError(
            f"{name} must be a string (got {type(value).__name__})", field=name,
        )
    if value not in choices:
        raise PolicyValidationError(
            f"{name} must be one of {list(choices)} (got {value!r})", field=name,
        )


_FIELD_CHECKS = {
    "operating_mode":                lambda v: _require_enum("operating_mode", v, VALID_OPERATING_MODES),
    "anomaly_response":              lambda v: _require_enum("anomaly_response", v, VALID_ANOMALY_RESPONSES),
    "safe_mode":                     lambda v: _require_bool("safe_mode", v),
    "min_backends":                  lambda v: _require_positive_int("min_backends", v),
    "max_backends":                  lambda v: _require_positive_int("max_backends", v),
    "slo_p95_latency_ms":            lambda v: _require_positive_int("slo_p95_latency_ms", v),
    "anomaly_recovery_window_seconds": lambda v: _require_positive_int("anomaly_recovery_window_seconds", v),
    "autoscaler_cooldown_seconds":   lambda v: _require_nonneg_number("autoscaler_cooldown_seconds", v),
    "per_instance_capacity_rps":     lambda v: _require_nonneg_number("per_instance_capacity_rps", v),
    "anomaly_latency_multiplier":    lambda v: _require_nonneg_number("anomaly_latency_multiplier", v),
    "rl_exploration_rate":           lambda v: _require_unit_interval("rl_exploration_rate", v),
    "rl_confidence_threshold":       lambda v: _require_unit_interval("rl_confidence_threshold", v),
}


# Canonical user-settable policy fields. POST bodies must use only these.
CANONICAL_POLICY_FIELDS: frozenset[str] = frozenset(_FIELD_CHECKS.keys())

# Server-managed envelope fields. Clients may echo these back when doing
# read-modify-write from a GET response, but they are stripped before merge —
# the server reassigns them on every write.
_SERVER_MANAGED_FIELDS: frozenset[str] = frozenset({
    "policy_version",
    "timestamp",
    "changed_fields",
})


# ── public API ────────────────────────────────────────────────────────────────

def validate_field(name: str, value: Any) -> None:
    """Validate a single field against its per-field rule. No-op for fields
    not in the canonical set — strict gating of unknown POST keys is the
    caller's responsibility (see `validate_updates`)."""
    check = _FIELD_CHECKS.get(name)
    if check is not None:
        check(value)


def validate_merged_policy(merged: dict) -> None:
    """Validate every known field in the merged policy + cross-field invariants.

    Call after merging the POST body into the existing policy. Raises
    PolicyValidationError on the first failure — the HTTP layer translates
    it into a 400 with the field name.

    Cross-field invariants (SOT §8.9 line 3002):
      - min_backends <= max_backends
    """
    for name, value in merged.items():
        validate_field(name, value)

    min_b = merged.get("min_backends")
    max_b = merged.get("max_backends")
    if isinstance(min_b, int) and isinstance(max_b, int) and min_b > max_b:
        raise PolicyValidationError(
            f"min_backends ({min_b}) must be <= max_backends ({max_b})",
            field="min_backends",
        )


def validate_updates(updates: dict, existing: dict) -> dict:
    """Validate a POST body against an existing policy. Returns the merged
    policy on success; raises PolicyValidationError on the first failure.

    Rejects any key not in `CANONICAL_POLICY_FIELDS` or `_SERVER_MANAGED_FIELDS`
    — surfaces caller bugs like an `actor` field in the body (which belongs in
    the `X-Actor` header) before they can leak to `config/policy.yaml`.
    Server-managed fields (`policy_version`, `timestamp`, `changed_fields`) are
    stripped silently so read-modify-write callers can echo a GET response back
    without a 400.

    The merge is "updates overrides existing" — unmentioned fields keep
    their existing values. Cross-field invariants are checked on the merged
    result so a POST that only changes max_backends still fails if it
    would leave min_backends > max_backends.
    """
    if not isinstance(updates, dict):
        raise PolicyValidationError("request body must be a JSON object")

    unknown = sorted(
        k for k in updates
        if k not in CANONICAL_POLICY_FIELDS and k not in _SERVER_MANAGED_FIELDS
    )
    if unknown:
        raise PolicyValidationError(
            f"unknown field(s) in POST body: {unknown} — not in the canonical policy schema",
            field=unknown[0],
        )

    user_updates = {k: v for k, v in updates.items() if k in CANONICAL_POLICY_FIELDS}

    for name, value in user_updates.items():
        validate_field(name, value)

    merged = {**existing, **user_updates}
    validate_merged_policy(merged)
    return merged
