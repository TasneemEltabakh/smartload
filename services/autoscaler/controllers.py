"""
services/autoscaler/controllers.py
───────────────────────────────────
Target-based scaling controllers — the principled successors to the shipped
``decisions.py::decide`` rule.

WHY A SECOND DECISION MODULE
────────────────────────────
The shipped ``decide()`` is a *bang-bang* rule: it moves the pool by exactly one
backend per action and only when the predicted load crosses the current
capacity. With a provisioning warm-up delay and a cooldown timer, that caps the
pool's *slew rate* at one instance per cooldown window — which is why every
strategy (even the perfect-foresight oracle) flat-lines at ~88 % SLA on a sharp
flash crowd: the demand needs +5 instances at once and the rule can only add one
at a time. See ``experiments/autoscaler-strategy-bench``.

The controllers here keep ``decide()`` untouched (it stays the production
default and the unit-tested reference) and add a richer family that:

  1. **sizes to a target count directly** — ``target = ceil(load / capacity)``
     plus a safety margin — and **jumps multiple steps in one action** (bounded
     by ``max_step_out``), removing the per-action slew cap;
  2. **separates scale-out from scale-in cooldown** (fast out, slow in) so the
     pool reacts to a spike immediately but drains conservatively, protecting
     the SLA without paying for permanent over-provisioning;
  3. **applies a deadband / hysteresis** on scale-in so per-step demand noise
     does not cause churn around a boundary.

Two sizing laws are provided:

  ``headroom``      target = ceil(load · (1 + headroom) / cap). A flat safety
                    margin; ``headroom`` is the single knob that trades SLA for
                    cost and traces out the Pareto frontier.

  ``sqrt_staffing`` square-root-staffing (Erlang-C / QED regime) staffing rule:
                    target = ceil(a + β·√a) where a = load/cap is the offered
                    load in instance-capacity units and β is a quality-of-
                    service constant. This is the call-centre staffing law: it
                    spends proportionally *more* slack at low load (where one
                    backend's granularity bites) and less at high load, which is
                    exactly the right shape for absorbing multiplicative demand
                    noise with a fixed service level.

Everything in this module is a pure function of its inputs — no I/O, no clock,
no Redis — so it is unit-testable from a single ``pytest`` invocation and
reusable from the strategy benchmark's ``sim.py`` exactly as ``decide()`` is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Reuse the shipped action vocabulary so audit logs and the dispatch in app.py
# do not need to learn a second set of constants.
from decisions import (  # type: ignore  # noqa: F401
    ACTION_SCALE_OUT,
    ACTION_SCALE_IN,
    ACTION_NOOP,
    Decision,
    Policy,
    decide,
)


@dataclass(frozen=True)
class ControlPolicy:
    """Policy for the target-based controllers.

    A superset of ``decisions.Policy``: the three sizing/bounds fields are the
    same, plus the asymmetric-cooldown, multi-step and sizing-law knobs.

    ``headroom``               fractional safety margin on predicted load
                               (0.15 = provision for 115 % of forecast).
    ``sizing``                 "headroom" | "sqrt_staffing".
    ``qos_beta``               β for the sqrt-staffing law (ignored otherwise).
    ``scale_out_cooldown_s``   min seconds between consecutive scale-OUT actions.
    ``scale_in_cooldown_s``    min seconds between consecutive scale-IN actions
                               (set larger than out → "fast out, slow in").
    ``max_step_out``           cap on instances added in one action (0 = no cap,
                               i.e. jump straight to the target).
    ``max_step_in``            cap on instances removed in one action (default 1
                               = drain one at a time, the conservative choice).
    ``scale_in_deadband``      extra fractional slack required before shedding:
                               only scale in if the post-shed pool still covers
                               load·(1 + headroom + scale_in_deadband). Prevents
                               flapping around the boundary.
    """

    min_backends: int
    max_backends: int
    per_instance_capacity_rps: float
    headroom: float = 0.15
    sizing: str = "headroom"
    qos_beta: float = 1.0
    scale_out_cooldown_s: float = 0.0
    scale_in_cooldown_s: float = 120.0
    max_step_out: int = 0
    max_step_in: int = 1
    scale_in_deadband: float = 0.15


def _clip(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def target_for_load(load_rps: float, policy: ControlPolicy) -> int:
    """Instances needed to serve ``load_rps`` under the policy's sizing law,
    clamped to [min_backends, max_backends].

    Pure and monotonic non-decreasing in ``load_rps`` — the property the
    benchmark and the unit tests rely on.
    """
    cap = policy.per_instance_capacity_rps
    if cap <= 0:
        return _clip(policy.min_backends, policy.min_backends, policy.max_backends)

    load = max(0.0, load_rps)
    if policy.sizing == "sqrt_staffing":
        # Offered load in "erlangs" of one-instance capacity, then the QED
        # square-root-staffing rule a + β√a. At a→0 this still demands ≥1.
        a = load / cap
        raw = a + policy.qos_beta * math.sqrt(a)
        need = math.ceil(raw)
    else:  # "headroom" (default)
        eff = load * (1.0 + policy.headroom)
        need = math.ceil(eff / cap)

    return _clip(int(need), policy.min_backends, policy.max_backends)


def decide_target(
    *,
    predicted_rps: float,
    current_count: int,
    policy: ControlPolicy,
    seconds_since_scale_out: float | None,
    seconds_since_scale_in: float | None,
    now_text: str = "forecast",
    offered_rps: float | None = None,
) -> Decision:
    """Return the scaling action for a target-based controller.

    ``seconds_since_scale_out`` / ``seconds_since_scale_in`` are the elapsed
    seconds since the last action *of that direction* (None = never), so the two
    cooldowns are tracked independently — a recent scale-in does not block an
    urgent scale-out. This is what makes "fast out, slow in" possible.

    Multi-step: a scale-out jumps straight to the sized target (capped by
    ``max_step_out`` if set); a scale-in moves at most ``max_step_in`` per action.

    ``offered_rps`` is an upper-band / arrivals estimate used to size the
    scale-OUT direction only. In a closed loop the point ``predicted_rps`` is
    learned from the *served* request rate, which collapses while the pool is
    shedding — so sizing scale-out on it alone lets the loop drain instead of
    grow (the shed-feedback trap). When ``offered_rps`` is supplied the out
    target is sized on ``max(predicted_rps, offered_rps)`` so a wide forecast
    band biases toward provisioning headroom and breaks the trap. Scale-IN and
    its deadband stay on ``predicted_rps`` so the conservative drain behaviour is
    unchanged. ``None`` (the default) reproduces the point-estimate contract.
    """
    if policy.per_instance_capacity_rps <= 0:
        return Decision(
            ACTION_NOOP,
            current_count,
            f"{now_text} predicted {predicted_rps:.0f} rps, but "
            f"per_instance_capacity_rps={policy.per_instance_capacity_rps:.3g} "
            f"is non-positive — refusing to scale on an invalid capacity",
        )

    # Scale-out sizes on the robust (offered/upper-band) signal so shedding can't
    # starve the loop; scale-in keeps sizing on the served point estimate.
    out_rps = predicted_rps if offered_rps is None else max(predicted_rps, offered_rps)
    out_target = target_for_load(out_rps, policy)
    target = target_for_load(predicted_rps, policy)
    cap = policy.per_instance_capacity_rps

    # ── scale OUT ─────────────────────────────────────────────────────────────
    if out_target > current_count:
        if (seconds_since_scale_out is not None
                and seconds_since_scale_out < policy.scale_out_cooldown_s):
            return Decision(
                ACTION_NOOP, current_count,
                f"{now_text} offered {out_rps:.0f} rps wants "
                f"{out_target} backends, scale-out cooldown active "
                f"({seconds_since_scale_out:.0f}s < {policy.scale_out_cooldown_s:.0f}s)",
            )
        step = out_target - current_count
        if policy.max_step_out > 0:
            step = min(step, policy.max_step_out)
        new = _clip(current_count + step, policy.min_backends, policy.max_backends)
        return Decision(
            ACTION_SCALE_OUT, new,
            f"{now_text} offered {out_rps:.0f} rps needs {out_target} "
            f"backends (have {current_count}); scaling out +{new - current_count}",
        )

    # ── scale IN ──────────────────────────────────────────────────────────────
    if target < current_count:
        # Deadband: only shed if the pool would still cover load with the full
        # headroom AND an extra slack band, so noise around the boundary does
        # not whipsaw the pool.
        shed_floor = predicted_rps * (1.0 + policy.headroom + policy.scale_in_deadband)
        if (current_count - 1) * cap < shed_floor:
            return Decision(
                ACTION_NOOP, current_count,
                f"{now_text} predicted {predicted_rps:.0f} rps wants {target} "
                f"backends but shedding one breaches the deadband — holding "
                f"{current_count}",
            )
        if (seconds_since_scale_in is not None
                and seconds_since_scale_in < policy.scale_in_cooldown_s):
            return Decision(
                ACTION_NOOP, current_count,
                f"{now_text} predicted {predicted_rps:.0f} rps wants {target} "
                f"backends, scale-in cooldown active "
                f"({seconds_since_scale_in:.0f}s < {policy.scale_in_cooldown_s:.0f}s)",
            )
        step = current_count - target
        if policy.max_step_in > 0:
            step = min(step, policy.max_step_in)
        new = _clip(current_count - step, policy.min_backends, policy.max_backends)
        return Decision(
            ACTION_SCALE_IN, new,
            f"{now_text} predicted {predicted_rps:.0f} rps needs {target} "
            f"backends (have {current_count}); scaling in -{current_count - new}",
        )

    # ── hold ──────────────────────────────────────────────────────────────────
    return Decision(
        ACTION_NOOP, current_count,
        f"{now_text} predicted {predicted_rps:.0f} rps matches {current_count} "
        f"backends — holding",
    )


# ── wiring helpers (app.py orchestration; pure, so unit-testable) ──────────────
#
# app.py owns the I/O (Redis, DB, Docker, Prometheus) and the live policy +
# cooldown clocks. These three helpers carry the decision/actuation maths that
# glue controllers.py into that loop, kept pure here so they are unit-testable
# without importing the service module.


def control_policy_from(
    policy: Policy,
    *,
    headroom: float,
    sizing: str,
    qos_beta: float,
    scale_out_cooldown_s: float,
    scale_in_cooldown_s: float,
    max_step_out: int,
    max_step_in: int,
    scale_in_deadband: float,
) -> ControlPolicy:
    """Project a live ``decisions.Policy`` onto a ``ControlPolicy``.

    The min/max/capacity bounds come from the live policy (so a runtime policy
    reload still moves them); the sizing law, asymmetric cooldowns, step caps
    and deadband are the deploy-time tuning passed by the caller.
    """
    return ControlPolicy(
        min_backends=policy.min_backends,
        max_backends=policy.max_backends,
        per_instance_capacity_rps=policy.per_instance_capacity_rps,
        headroom=headroom,
        sizing=sizing,
        qos_beta=qos_beta,
        scale_out_cooldown_s=scale_out_cooldown_s,
        scale_in_cooldown_s=scale_in_cooldown_s,
        max_step_out=max_step_out,
        max_step_in=max_step_in,
        scale_in_deadband=scale_in_deadband,
    )


def select_decision(
    kind: str,
    *,
    predicted_rps: float,
    current_count: int,
    step_policy: Policy,
    control_policy: ControlPolicy,
    seconds_since_last_action: float | None,
    seconds_since_scale_out: float | None,
    seconds_since_scale_in: float | None,
    now_text: str = "forecast",
    offered_rps: float | None = None,
    scale_in_confirmations_seen: int = 1,
) -> Decision:
    """Dispatch to the configured controller.

    ``kind == "target"`` uses ``decide_target`` with the two per-direction
    cooldown clocks; anything else uses the shipped ``decide`` with the single
    action clock. The caller passes both policies and all three clocks so this
    stays a pure function of its inputs.

    ``offered_rps`` (the forecast upper-band / arrivals estimate) sizes the
    target controller's scale-OUT direction; the shipped ``step`` rule now also
    consumes it as its anti-flap demand signal for BOTH directions (so scale-in
    only fires when demand genuinely dropped). ``scale_in_confirmations_seen``
    drives the step rule's scale-in hysteresis (ignored by the target rule,
    which has its own deadband). Both are backwards-compatible: ``None`` /
    default ``1`` reproduce the original point-estimate, act-on-first-reading
    contract.
    """
    if kind == "target":
        return decide_target(
            predicted_rps=predicted_rps,
            current_count=current_count,
            policy=control_policy,
            seconds_since_scale_out=seconds_since_scale_out,
            seconds_since_scale_in=seconds_since_scale_in,
            now_text=now_text,
            offered_rps=offered_rps,
        )
    return decide(
        predicted_rps=predicted_rps,
        current_count=current_count,
        policy=step_policy,
        seconds_since_last_action=seconds_since_last_action,
        now_text=now_text,
        offered_rps=offered_rps,
        scale_in_confirmations_seen=scale_in_confirmations_seen,
    )


def actuate_to_target(
    action: str,
    current_count: int,
    target_count: int,
    scale_fn,
) -> tuple[int, int, str | None, str | None]:
    """Drive ``current_count`` toward ``target_count`` one instance at a time.

    ``scale_fn`` is the cluster method for the action's direction; it returns
    ``(name, mechanism)`` on a successful actuation or ``None`` when the cluster
    can no longer add/remove a backend. Stops at the target or at the first
    ``None`` (an exhausted pool), so a multi-step jump that can only be partly
    served records the count actually reached.

    Returns ``(actuated, final_count, last_name, last_mechanism)``. For a NOOP
    or an already-met target, ``actuated`` is 0 and ``final_count`` is
    ``current_count``.
    """
    steps = abs(target_count - current_count)
    actuated = 0
    last_name: str | None = None
    last_mechanism: str | None = None
    for _ in range(steps):
        result = scale_fn()
        if result is None:
            break
        last_name, last_mechanism = result
        actuated += 1

    if action == ACTION_SCALE_OUT:
        final_count = current_count + actuated
    elif action == ACTION_SCALE_IN:
        final_count = current_count - actuated
    else:
        final_count = current_count
    return actuated, final_count, last_name, last_mechanism
