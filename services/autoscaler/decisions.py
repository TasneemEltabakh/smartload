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
    # ── anti-flap knobs (issue: backend pool oscillates under sustained load) ──
    # The shipped step rule sized BOTH directions on the served/point predicted
    # rate. Under overload the served rate is DEPRESSED (the pool is shedding),
    # so right after a scale-out the depressed reading made decide() want to
    # scale back in → flap. Two complementary, opt-in levers fix this; both
    # default to a value that PRESERVES the original behaviour when unset.
    #
    # scale_in_cooldown_seconds: a downscale-specific cooldown. Scale-IN must
    #   wait this long since the last action (typically longer than the generic
    #   cooldown → "fast out, slow in"), so one low reading right after a
    #   scale-out cannot immediately shrink the pool. <= 0 (default) means "fall
    #   back to cooldown_seconds" — i.e. the original single-cooldown behaviour.
    scale_in_cooldown_seconds: float = 0.0
    # scale_in_confirmations: how many CONSECUTIVE ticks demand must stay below
    #   the shed threshold before a scale-in actually fires (hysteresis). 1
    #   (default) = act on the first qualifying reading, i.e. original behaviour.
    scale_in_confirmations: int = 1


def policy_from_payload(payload: dict, fallback: Policy) -> Policy:
    """Build a Policy from a smartload.policy PolicyUpdate payload.

    Only fields that affect autoscaling decisions are pulled. Unknown fields
    are ignored (forward-compat). Missing fields fall back to `fallback`, so
    a partial-snapshot publish does not zero out scaling bounds.

    Pure function — no I/O, no lock — so it can be unit-tested alongside
    `decide` without spinning up the autoscaler.
    """
    def _int(key: str, default: int) -> int:
        v = payload.get(key, default)
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def _float(key: str, default: float) -> float:
        v = payload.get(key, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    return Policy(
        min_backends=_int("min_backends", fallback.min_backends),
        max_backends=_int("max_backends", fallback.max_backends),
        per_instance_capacity_rps=_float(
            "per_instance_capacity_rps", fallback.per_instance_capacity_rps,
        ),
        cooldown_seconds=_float(
            "autoscaler_cooldown_seconds", fallback.cooldown_seconds,
        ),
        # Anti-flap knobs — also live-reloadable. Absent keys fall back to the
        # current policy, so a partial publish keeps the operator's tuning.
        scale_in_cooldown_seconds=_float(
            "autoscaler_scale_in_cooldown_seconds", fallback.scale_in_cooldown_seconds,
        ),
        scale_in_confirmations=_int(
            "autoscaler_scale_in_confirmations", fallback.scale_in_confirmations,
        ),
    )


@dataclass(frozen=True)
class Decision:
    action: str        # ACTION_SCALE_OUT | ACTION_SCALE_IN | ACTION_NOOP
    target_count: int  # what the backend count will be after the action
    reason: str        # human-readable justification (also persisted to DB)


def scale_in_cooldown(policy: Policy) -> float:
    """Effective downscale cooldown. Falls back to the generic cooldown when the
    downscale-specific knob is unset (<= 0), preserving original behaviour."""
    return (
        policy.scale_in_cooldown_seconds
        if policy.scale_in_cooldown_seconds > 0
        else policy.cooldown_seconds
    )


def decide(
    *,
    predicted_rps: float,
    current_count: int,
    policy: Policy,
    seconds_since_last_action: float | None,
    now_text: str = "forecast",
    offered_rps: float | None = None,
    scale_in_confirmations_seen: int = 1,
) -> Decision:
    """Return the action the autoscaler should take for this signal.

    `seconds_since_last_action` = None means there has been no prior action
    in this process's lifetime (e.g. fresh boot); cooldown does not apply.
    `now_text` is a label embedded in the `reason` field so audit log entries
    distinguish forecast-driven from reactive-fallback decisions.

    Anti-flap (consistent demand signal — lever 1)
    ──────────────────────────────────────────────
    `offered_rps` is the arrival/offered demand estimate (e.g. the forecast
    confidence-upper band). When supplied, scale-OUT sizes on
    max(predicted_rps, offered_rps) and — crucially — **scale-IN also tests
    against offered_rps**, so the pool only shrinks when *demand actually
    dropped*, not merely because the served/predicted rate is depressed by an
    overloaded pool shedding requests. `None` (default) reproduces the original
    point-estimate contract: both directions size on predicted_rps.

    Anti-flap (hysteresis — lever 2)
    ────────────────────────────────
    `scale_in_confirmations_seen` is the number of CONSECUTIVE ticks (including
    this one) on which demand has qualified for scale-in. A scale-IN only fires
    once this reaches `policy.scale_in_confirmations`. The caller is responsible
    for counting consecutive qualifying ticks and resetting on any non-qualifying
    reading. Default 1 with default `scale_in_confirmations=1` ⇒ act on the
    first reading (original behaviour). Combined with the downscale-specific
    cooldown (`scale_in_cooldown_seconds`), a single low reading right after a
    scale-out cannot immediately shrink the pool.
    """
    # The demand signal that drives SHRINK decisions: the same arrival/offered
    # estimate scale-out uses, so "we're serving less because we're overloaded"
    # can no longer masquerade as "demand dropped". Falls back to predicted_rps.
    demand_rps = predicted_rps if offered_rps is None else max(predicted_rps, offered_rps)
    if policy.per_instance_capacity_rps <= 0:
        return Decision(
            ACTION_NOOP,
            current_count,
            f"{now_text} predicted {predicted_rps:.0f} rps, but "
            f"per_instance_capacity_rps={policy.per_instance_capacity_rps:.3g} "
            f"is non-positive — refusing to scale on an invalid capacity",
        )

    capacity = current_count * policy.per_instance_capacity_rps

    # Scale-OUT sizes on the offered/arrival demand (robust to shedding).
    if demand_rps > capacity:
        if current_count >= policy.max_backends:
            return Decision(
                ACTION_NOOP,
                current_count,
                f"{now_text} predicted {demand_rps:.0f} rps > capacity "
                f"{capacity:.0f}, but already at max_backends={policy.max_backends}",
            )
        if seconds_since_last_action is not None and seconds_since_last_action < policy.cooldown_seconds:
            return Decision(
                ACTION_NOOP,
                current_count,
                f"{now_text} predicted {demand_rps:.0f} rps > capacity "
                f"{capacity:.0f}, cooldown active "
                f"({seconds_since_last_action:.0f}s < {policy.cooldown_seconds:.0f}s)",
            )
        return Decision(
            ACTION_SCALE_OUT,
            current_count + 1,
            f"{now_text} predicted {demand_rps:.0f} rps > capacity {capacity:.0f}",
        )

    shed_capacity = (current_count - 1) * policy.per_instance_capacity_rps
    # Scale-IN tests against the SAME offered/arrival demand as scale-out, so the
    # pool only shrinks when demand genuinely dropped — not because the served
    # rate is depressed by an overloaded pool. (anti-flap lever 1)
    if demand_rps < shed_capacity:
        if current_count <= policy.min_backends:
            return Decision(
                ACTION_NOOP,
                current_count,
                f"{now_text} predicted {demand_rps:.0f} rps < shed-capacity "
                f"{shed_capacity:.0f}, but already at min_backends={policy.min_backends}",
            )
        # Downscale-specific cooldown ("slow in"): a single low reading right
        # after a scale-out can't immediately shrink the pool. (anti-flap lever 2a)
        in_cooldown = scale_in_cooldown(policy)
        if seconds_since_last_action is not None and seconds_since_last_action < in_cooldown:
            return Decision(
                ACTION_NOOP,
                current_count,
                f"{now_text} predicted {demand_rps:.0f} rps < shed-capacity "
                f"{shed_capacity:.0f}, scale-in cooldown active "
                f"({seconds_since_last_action:.0f}s < {in_cooldown:.0f}s)",
            )
        # Confirmation hysteresis: demand must stay below the shed threshold for
        # `scale_in_confirmations` consecutive ticks before we actually shrink.
        # (anti-flap lever 2b)
        if scale_in_confirmations_seen < policy.scale_in_confirmations:
            return Decision(
                ACTION_NOOP,
                current_count,
                f"{now_text} predicted {demand_rps:.0f} rps < shed-capacity "
                f"{shed_capacity:.0f}, awaiting scale-in confirmation "
                f"({scale_in_confirmations_seen}/{policy.scale_in_confirmations})",
            )
        return Decision(
            ACTION_SCALE_IN,
            current_count - 1,
            f"{now_text} predicted {demand_rps:.0f} rps < shed-capacity {shed_capacity:.0f}",
        )

    return Decision(
        ACTION_NOOP,
        current_count,
        f"{now_text} predicted {demand_rps:.0f} rps within band "
        f"[{shed_capacity:.0f}, {capacity:.0f}]",
    )
