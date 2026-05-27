"""
services/rl-engine/runloop.py
──────────────────────────────
Pure-Python pieces of the rl-engine run loop, separated from app.py so
they can be unit-tested without Flask, Redis, or a DB connection.

The Flask entry point (app.py) owns:
  - sockets and threads
  - the actual psycopg2 + redis clients
  - request/response handling

This module owns:
  - select-with-fallback policy bootstrap
  - building a list[BackendState] from an RL_STATE_QUERY result set
  - composing the published mode from policy intent + RL_MODE env + safe_mode
  - converting RoutingAction → RoutingRecommendation envelope payload
  - policy payload → policy_base.RoutingPolicy kwargs

The decision-plane analog of services/anomaly-detector/runloop.py and
services/forecasting/runloop.py. Same shape; the only differences are
the policy_base ABC (instead of engine_base), the policies/ plugin
folder (instead of engines/), and the operator-mode gate that lets the
operator pin the published `mode` field to "shadow" even when a trained
policy would have chosen "active".
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

# Make policy_base + plugin folders importable when this file is loaded
# from /app (container) or services/rl-engine/ (dev).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from policy_base import (  # noqa: E402
    BackendState,
    RoutingAction,
    RoutingPolicy,
    select_policy,
)


# ── health classification thresholds ─────────────────────────────────────────
# RL_STATE_QUERY returns latency / request_count / error_rate but not the
# canonical health flag (anomaly-detector owns that write to backend_health,
# and #138 round 1 publishes only to Redis — the DB write is a future
# extension). Until then, derive health locally with the same thresholds the
# threshold engine uses so the two services agree on what "degraded" means.

DEGRADED_LATENCY_MS    = 200.0   # matches SOT §19 P95 SLO target
UNHEALTHY_ERROR_RATE   = 0.05    # matches anomaly-detector default


def classify_health(latency_ms: float, error_rate: float) -> str:
    """Map raw metrics to one of healthy / degraded / unhealthy."""
    if error_rate > UNHEALTHY_ERROR_RATE:
        return "unhealthy"
    if latency_ms > DEGRADED_LATENCY_MS:
        return "degraded"
    return "healthy"


# ── policy-derived constructor kwargs ────────────────────────────────────────

DEFAULT_RL_CONFIDENCE_THRESHOLD = 0.6
DEFAULT_RL_EXPLORATION_RATE     = 0.0


@dataclass
class EnginePolicy:
    """Subset of the operating policy that drives the RL policy.

    Built from a smartload.policy envelope payload or from defaults at
    startup. The RL policy's constructor consumes whichever kwargs match
    its signature — random_shadow ignores all of these; a trained PPO
    plugin can read rl_exploration_rate / rl_confidence_threshold /
    operating_mode.
    """
    rl_confidence_threshold: float = DEFAULT_RL_CONFIDENCE_THRESHOLD
    rl_exploration_rate:     float = DEFAULT_RL_EXPLORATION_RATE
    safe_mode:               bool  = False
    policy_version:          int   = 0
    operating_mode:          str   = "shadow"  # "shadow" | "hybrid" | "learning"

    def policy_kwargs(self) -> dict:
        """Kwargs passed to select_policy(). Policies that don't take these
        constructor params (random_shadow takes only `seed`) ignore them."""
        return {
            "confidence_threshold": self.rl_confidence_threshold,
            "exploration_rate":     self.rl_exploration_rate,
            "operating_mode":       self.operating_mode,
        }


def policy_from_payload(payload: dict, fallback: EnginePolicy) -> EnginePolicy:
    """Build an EnginePolicy from a smartload.policy envelope payload.

    Fields missing or with wrong types fall back to the previous values so
    a malformed publish never wipes the live policy.
    """
    def _float(key: str, default: float) -> float:
        try:
            return float(payload.get(key, default))
        except (TypeError, ValueError):
            return default

    def _int(key: str, default: int) -> int:
        try:
            return int(payload.get(key, default))
        except (TypeError, ValueError):
            return default

    # Map "classical" → "shadow" for backwards-compat with policy.yaml values
    # that predate the operating_mode field (Amendment B).
    _raw_mode = payload.get("operating_mode", fallback.operating_mode)
    if _raw_mode == "classical":
        _op_mode = "shadow"
    elif _raw_mode in ("shadow", "hybrid", "learning"):
        _op_mode = _raw_mode
    else:
        _op_mode = fallback.operating_mode

    return EnginePolicy(
        rl_confidence_threshold=_float("rl_confidence_threshold",
                                       fallback.rl_confidence_threshold),
        rl_exploration_rate=_float("rl_exploration_rate",
                                   fallback.rl_exploration_rate),
        safe_mode=bool(payload.get("safe_mode", fallback.safe_mode)),
        policy_version=_int("policy_version", fallback.policy_version),
        operating_mode=_op_mode,
    )


# ── policy bootstrap with safety-net fallback ────────────────────────────────

BASELINE_POLICY_NAME = "random_shadow"


@dataclass
class PolicyBootstrap:
    """Outcome of policy startup. ready=False means we fell back to the baseline."""
    policy:    RoutingPolicy
    name:      str          # the policy that's actually loaded
    requested: str          # the policy the operator asked for via RL_POLICY
    ready:     bool         # True iff the requested policy loaded
    error:     str | None   # exception message when ready=False


def _safe_select(name: str, kwargs: dict) -> RoutingPolicy:
    """Try select_policy(name, **kwargs). Fall back to no-kwargs if the
    chosen policy doesn't accept those constructor params (random_shadow
    takes only `seed`; passing it confidence_threshold raises TypeError)."""
    try:
        return select_policy(name, **kwargs)
    except TypeError:
        return select_policy(name)


def bootstrap_policy(requested: str, policy: EnginePolicy) -> PolicyBootstrap:
    """Try the requested policy; on any load failure, fall back to the baseline.

    The baseline (random_shadow) has no model artifact and never fails, so
    it's the safety net. If the baseline itself fails, the exception
    propagates — that's a deployment bug, not something to swallow.
    """
    try:
        p = _safe_select(requested, policy.policy_kwargs())
        return PolicyBootstrap(policy=p, name=requested, requested=requested,
                               ready=True, error=None)
    except Exception as exc:                                # noqa: BLE001
        if requested == BASELINE_POLICY_NAME:
            raise
        baseline = _safe_select(BASELINE_POLICY_NAME, policy.policy_kwargs())
        return PolicyBootstrap(policy=baseline, name=BASELINE_POLICY_NAME,
                               requested=requested, ready=False, error=str(exc))


# ── DB rows → backend state ───────────────────────────────────────────────────

# RL_STATE_QUERY shape: (instance, latency, request_count, error_rate).
# We convert to one BackendState per backend.

def build_state_from_rows(
    rows: list[tuple],
    anomaly_health: dict[str, str] | None = None,
) -> list[BackendState]:
    """Convert RL_STATE_QUERY rows into a list[BackendState].

    When anomaly_health is provided the anomaly detector's verdict for each
    backend_id takes precedence over local classification (SOT §9 health
    ownership rule). Local classify_health() is the fallback for backends
    that have not yet appeared on smartload.anomaly.
    """
    states: list[BackendState] = []
    for row in rows:
        try:
            instance, latency, request_count, error_rate = row
        except ValueError:
            continue   # malformed row — skip, don't poison the batch

        latency_ms = float(latency) if latency is not None else 0.0
        queue_depth = int(request_count) if request_count is not None else 0
        err_rate = float(error_rate) if error_rate is not None else 0.0

        if anomaly_health is not None and instance in anomaly_health:
            health = anomaly_health[instance]
        else:
            health = classify_health(latency_ms, err_rate)

        states.append(BackendState(
            backend_id=instance,
            latency_ms=latency_ms,
            queue_depth=queue_depth,
            health=health,
        ))
    return states


# ── mode composition + publish gate ──────────────────────────────────────────

SHADOW = "shadow"
ACTIVE = "active"


def effective_mode(action_mode: str, rl_mode_env: str, policy: EnginePolicy) -> str:
    """Compose the published `mode` field from three inputs.

    Rules:
      - policy.safe_mode=true               → always "shadow", regardless of
                                              policy or env
      - RL_MODE env != "active"             → "shadow" (operator pin)
      - both RL_MODE="active" and the policy
        returned mode="active"              → "active"
      - any other combination               → "shadow"

    This keeps RL output observable while denying it routing authority unless
    every gate agrees.
    """
    if policy.safe_mode:
        return SHADOW
    if rl_mode_env.lower() != ACTIVE:
        return SHADOW
    if action_mode == ACTIVE:
        return ACTIVE
    return SHADOW


def should_publish(state: list[BackendState]) -> bool:
    """Decide whether the cycle's action becomes a published envelope.

    Rule:
      - empty state (cold DB, idle stack) → don't publish; nothing for the
        sidecar to act on, and an empty server_rankings list is meaningless
      - otherwise → publish; even shadow recommendations are valuable signal
        for explainability + the operator UI live-engines view (#121)
    """
    return len(state) > 0


def action_to_event_payload(action: RoutingAction, mode: str,
                            policy_version: int) -> dict:
    """Serialise a RoutingAction + effective mode into the
    RoutingRecommendation dict shape expected by publish_envelope.

    policy_version on the envelope is the join key back to the
    PolicyUpdate snapshot RL was reasoning under — useful for downstream
    audit ("what policy was in effect when this routing was recommended?").
    """
    return {
        "mode": mode,
        "server_rankings": [
            {"backend_id": r.backend_id, "score": r.score}
            for r in action.rankings
        ],
        "policy_version": policy_version,
    }
