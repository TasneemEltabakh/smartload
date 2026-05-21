"""
services/autoscaler/manual.py
──────────────────────────────
Pure-Python plan logic for POST /api/v1/scale (manual override).

Lives next to decisions.py so app.py can mix automatic decisions (forecast /
reactive) with operator overrides without duplicating bounds-validation.
The plan function never talks to Docker, Redis, or the DB — those side
effects happen in app.py against the plan it returns.

Manual scales explicitly **bypass cooldown** (operator intent overrides
debounce) but still **respect min / max bounds** (those are policy
contracts, not debounce signals).
"""

from __future__ import annotations

from dataclasses import dataclass

from decisions import ACTION_NOOP, ACTION_SCALE_IN, ACTION_SCALE_OUT, Policy


class ManualScaleError(ValueError):
    """Raised when a manual scale request can't be validated.

    The `field` attribute names the offending input — the HTTP layer
    surfaces this as `{"error": ..., "field": ...}` 400 so SDK consumers
    can map back to a `ValidationError(field=...)`.
    """
    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.message = message
        self.field = field


@dataclass(frozen=True)
class ManualScalePlan:
    """How many cluster_client.scale_* calls to make to reach target_count.

    `steps == 0` means the cluster is already at target (action == noop).
    The HTTP layer still writes an audit row + publishes an envelope on
    noop so operators see their intent in the log.
    """
    action: str            # ACTION_SCALE_OUT | ACTION_SCALE_IN | ACTION_NOOP
    steps: int             # number of single-step cluster calls
    target_count: int      # resulting backend count after the steps
    reason: str            # audited reason — prefixed manual:<actor>: <user reason>


def plan_manual_scale(
    *,
    target_count,           # parsed from JSON — could be anything
    current_count: int,
    policy: Policy,
    actor: str,
    user_reason: str | None,
) -> ManualScalePlan:
    """Validate target + compose a plan.

    Validation:
      - target_count must coerce to a non-negative integer
      - policy.min_backends <= target_count <= policy.max_backends

    Direction:
      - target > current  → SCALE_OUT for (target - current) steps
      - target < current  → SCALE_IN for (current - target) steps
      - target == current → NOOP (audited as such)

    The reason is composed from the operator-supplied string so it's
    grep-able in the audit log: `manual:<actor>: <user_reason>`. When
    user_reason is empty/missing it falls back to "manual override".
    """
    try:
        target_int = int(target_count)
    except (TypeError, ValueError):
        raise ManualScaleError(
            f"target_count must be an integer, got {target_count!r}",
            field="target_count",
        )
    if target_int < 0:
        raise ManualScaleError(
            f"target_count must be >= 0, got {target_int}",
            field="target_count",
        )
    if target_int < policy.min_backends:
        raise ManualScaleError(
            f"target_count {target_int} below policy.min_backends "
            f"({policy.min_backends})",
            field="target_count",
        )
    if target_int > policy.max_backends:
        raise ManualScaleError(
            f"target_count {target_int} above policy.max_backends "
            f"({policy.max_backends})",
            field="target_count",
        )

    safe_actor = (actor or "operator").strip() or "operator"
    safe_user_reason = (user_reason or "manual override").strip() or "manual override"
    audited_reason = f"manual:{safe_actor}: {safe_user_reason}"

    if target_int > current_count:
        return ManualScalePlan(
            action=ACTION_SCALE_OUT,
            steps=target_int - current_count,
            target_count=target_int,
            reason=audited_reason,
        )
    if target_int < current_count:
        return ManualScalePlan(
            action=ACTION_SCALE_IN,
            steps=current_count - target_int,
            target_count=target_int,
            reason=audited_reason,
        )
    return ManualScalePlan(
        action=ACTION_NOOP,
        steps=0,
        target_count=target_int,
        reason=audited_reason,
    )
