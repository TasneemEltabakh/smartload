"""
services/autoscaler/decisions.py
─────────────────────────────────
Pure scale-decision logic for the autoscaler. No I/O, no Redis, no Docker —
everything in here is unit-testable from a single `pytest` invocation.

Per SOT §8.8 (Logic):
    if predicted_rps > current_backends × per_instance_capacity_rps:
        scale_out(1)                                  # respect max_backends
    elif predicted_rps < (current_backends − 1) × per_instance_capacity_rps:
        scale_in(1)                                   # respect min_backends
    respect autoscaler_cooldown_seconds

Lower-bound interpretation: scale_in only when shedding one backend still
leaves capacity above demand. This mirrors the "we have one too many"
formulation and avoids hysteresis tuning. The cooldown timer already prevents
oscillation around the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

ACTION_SCALE_OUT = "scale_out"
ACTION_SCALE_IN  = "scale_in"
ACTION_NOOP      = "noop"


@dataclass(frozen=True)
class Policy:
    min_backends: int
    max_backends: int
    per_instance_capacity_rps: float
    cooldown_seconds: float


@dataclass(frozen=True)
class Decision:
    action: str        # ACTION_SCALE_OUT | ACTION_SCALE_IN | ACTION_NOOP
    target_count: int  # what the backend count will be after the action
    reason: str        # human-readable justification (also persisted to DB)


def decide(
    *,
    predicted_rps: float,
    current_count: int,
    policy: Policy,
    seconds_since_last_action: float | None,
    now_text: str = "forecast",
) -> Decision:
    """Return the action the autoscaler should take for this signal.

    `seconds_since_last_action` = None means there has been no prior action
    in this process's lifetime (e.g. fresh boot); cooldown does not apply.
    `now_text` is a label embedded in the `reason` field so audit log entries
    distinguish forecast-driven from reactive-fallback decisions.
    """
    capacity = current_count * policy.per_instance_capacity_rps

    if predicted_rps > capacity:
        if current_count >= policy.max_backends:
            return Decision(
                ACTION_NOOP,
                current_count,
                f"{now_text} predicted {predicted_rps:.0f} rps > capacity "
                f"{capacity:.0f}, but already at max_backends={policy.max_backends}",
            )
        if seconds_since_last_action is not None and seconds_since_last_action < policy.cooldown_seconds:
            return Decision(
                ACTION_NOOP,
                current_count,
                f"{now_text} predicted {predicted_rps:.0f} rps > capacity "
                f"{capacity:.0f}, cooldown active "
                f"({seconds_since_last_action:.0f}s < {policy.cooldown_seconds:.0f}s)",
            )
        return Decision(
            ACTION_SCALE_OUT,
            current_count + 1,
            f"{now_text} predicted {predicted_rps:.0f} rps > capacity {capacity:.0f}",
        )

    shed_capacity = (current_count - 1) * policy.per_instance_capacity_rps
    if predicted_rps < shed_capacity:
        if current_count <= policy.min_backends:
            return Decision(
                ACTION_NOOP,
                current_count,
                f"{now_text} predicted {predicted_rps:.0f} rps < shed-capacity "
                f"{shed_capacity:.0f}, but already at min_backends={policy.min_backends}",
            )
        if seconds_since_last_action is not None and seconds_since_last_action < policy.cooldown_seconds:
            return Decision(
                ACTION_NOOP,
                current_count,
                f"{now_text} predicted {predicted_rps:.0f} rps < shed-capacity "
                f"{shed_capacity:.0f}, cooldown active "
                f"({seconds_since_last_action:.0f}s < {policy.cooldown_seconds:.0f}s)",
            )
        return Decision(
            ACTION_SCALE_IN,
            current_count - 1,
            f"{now_text} predicted {predicted_rps:.0f} rps < shed-capacity {shed_capacity:.0f}",
        )

    return Decision(
        ACTION_NOOP,
        current_count,
        f"{now_text} predicted {predicted_rps:.0f} rps within band "
        f"[{shed_capacity:.0f}, {capacity:.0f}]",
    )
