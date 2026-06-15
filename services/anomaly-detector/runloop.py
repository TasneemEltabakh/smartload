"""
services/anomaly-detector/runloop.py
─────────────────────────────────────
Pure-Python pieces of the anomaly-detector run loop, separated from app.py
so they can be unit-tested without Flask, Redis, or a DB connection.

The Flask entry point (app.py) owns:
  - sockets and threads
  - the actual psycopg2 + redis clients
  - request/response handling

This module owns:
  - select-with-fallback engine bootstrap
  - building BackendFeatures from a DB result set
  - converting AnomalyScore → AnomalyEvent envelope payload
  - policy payload → engine kwargs
  - the "should I publish?" gate that respects safe_mode + advisory mode
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import asdict, dataclass

# Make engine_base + plugin folders importable when this file is loaded from
# /app (container) or from services/anomaly-detector/ (dev).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from engine_base import (  # noqa: E402
    AnomalyEngine,
    AnomalyScore,
    BackendFeatures,
    select_engine,
)


# ── policy-derived engine kwargs ──────────────────────────────────────────────

DEFAULT_LATENCY_MULTIPLIER = 3.0
DEFAULT_ERROR_RATE_THRESHOLD = 0.05
DEFAULT_MIN_SAMPLE_COUNT = 10
DEFAULT_FLIP_CONFIRMATION_CYCLES = 2


@dataclass
class EnginePolicy:
    """Subset of the operating policy that drives the anomaly engine.

    Built from a smartload.policy envelope payload (or from defaults at
    startup). Passed as kwargs to select_engine() so any engine that takes
    matching constructor params picks them up; engines that don't are free
    to ignore them.
    """
    latency_multiplier: float = DEFAULT_LATENCY_MULTIPLIER
    error_rate_threshold: float = DEFAULT_ERROR_RATE_THRESHOLD
    min_sample_count: int = DEFAULT_MIN_SAMPLE_COUNT
    safe_mode: bool = False
    anomaly_response: str = "auto-isolate"   # "auto-isolate" | "advisory"
    policy_version: int = 0
    # Cycles a raw status change must persist before apply_stability_gate()
    # confirms it (B2 fix). Not an engine constructor param -- excluded from
    # engine_kwargs(), consumed directly by app.py's _inference_cycle.
    flip_confirmation_cycles: int = DEFAULT_FLIP_CONFIRMATION_CYCLES

    def engine_kwargs(self) -> dict:
        return {
            "latency_multiplier":    self.latency_multiplier,
            "error_rate_threshold":  self.error_rate_threshold,
            "min_sample_count":      self.min_sample_count,
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

    return EnginePolicy(
        latency_multiplier=_float("anomaly_latency_multiplier", fallback.latency_multiplier),
        error_rate_threshold=fallback.error_rate_threshold,
        min_sample_count=fallback.min_sample_count,
        safe_mode=bool(payload.get("safe_mode", fallback.safe_mode)),
        anomaly_response=str(payload.get("anomaly_response", fallback.anomaly_response)),
        policy_version=_int("policy_version", fallback.policy_version),
        flip_confirmation_cycles=_int("anomaly_flip_confirmation_cycles", fallback.flip_confirmation_cycles),
    )


# ── engine bootstrap with safety-net fallback ────────────────────────────────

BASELINE_ENGINE_NAME = "threshold"


@dataclass
class EngineBootstrap:
    """Outcome of engine startup. ready=False means we fell back to the baseline."""
    engine: AnomalyEngine
    name: str             # the engine that's actually loaded (may differ from `requested`)
    requested: str        # the engine the operator asked for via ANOMALY_ENGINE
    ready: bool           # True iff the requested engine loaded; False after fallback
    error: str | None     # exception message when ready=False


def bootstrap_engine(requested: str, policy: EnginePolicy) -> EngineBootstrap:
    """Try the requested engine; on any load failure, fall back to the baseline.

    The baseline (threshold) has no model artifact and never fails, so it's
    the safety net. If the baseline itself fails, the exception propagates —
    that's a deployment bug, not something to swallow.
    """
    try:
        engine = select_engine(requested, **policy.engine_kwargs())
        return EngineBootstrap(engine=engine, name=requested, requested=requested,
                               ready=True, error=None)
    except Exception as exc:                                # noqa: BLE001
        if requested == BASELINE_ENGINE_NAME:
            raise
        baseline = select_engine(BASELINE_ENGINE_NAME, **policy.engine_kwargs())
        return EngineBootstrap(engine=baseline, name=BASELINE_ENGINE_NAME,
                               requested=requested, ready=False, error=str(exc))


# ── DB rows → features ────────────────────────────────────────────────────────

# Instances that are NOT real backends and must never be scored as one.
#
# NGINX records the upstream *block name* `backend_pool` as `$upstream_addr`
# when no live `server` is reachable (the all-down 502 sentinel), and the
# lb-otel-shipper emits `unknown` when NGINX never reached an upstream at all.
# Both leak into the metrics stream as an `instance` with a 100% error_rate
# during any 502 window. Scoring them like a backend yields a phantom
# `unhealthy` verdict that the lb-sidecar then "excludes" — which keeps the
# pool empty, which 502s every request, which re-fires the verdict: a
# self-sustaining outage (see audit/_findings/anomaly-pool-collapse-rootcause).
# They are dropped here so the engine only ever scores real backends.
NON_BACKEND_INSTANCES = frozenset({"backend_pool", "unknown"})


# ANOMALY_QUERY shape: one row per (instance, metric_name) pair, with columns
#   (instance, metric_name, avg_value, max_value, std_value, sample_count)
# We pivot it into one BackendFeatures per instance.

def build_features_from_rows(rows: list[tuple]) -> list[BackendFeatures]:
    """Pivot ANOMALY_QUERY rows into one BackendFeatures per backend.

    Rows are tuples: (instance, metric_name, avg, max, std, sample_count).
    Returns an empty list when no rows arrive (cold DB, idle stack).

    Non-backend instances (`NON_BACKEND_INSTANCES` — the NGINX all-down
    sentinel and the shipper's no-upstream fallback) are skipped so the
    load-balancer aggregate is never scored as if it were a backend.
    """
    by_instance: dict[str, dict] = {}
    for row in rows:
        try:
            instance, metric_name, avg, mx, std, samples = row
        except ValueError:
            continue   # malformed row — skip, don't poison the whole batch
        if instance in NON_BACKEND_INSTANCES:
            continue   # LB aggregate / no-upstream sentinel — not a real backend
        entry = by_instance.setdefault(instance, {})
        entry[metric_name] = {
            "avg":     float(avg) if avg is not None else 0.0,
            "max":     float(mx)  if mx  is not None else 0.0,
            "std":     float(std) if std is not None else 0.0,
            "samples": int(samples) if samples is not None else 0,
        }

    features: list[BackendFeatures] = []
    for instance, metrics in by_instance.items():
        latency = metrics.get("request_latency_ms", {})
        errors  = metrics.get("error_rate", {})
        # Total samples for the gate — sum across metric names since each is a
        # separate row in the query result.
        sample_count = max(latency.get("samples", 0), errors.get("samples", 0))
        features.append(BackendFeatures(
            backend_id=instance,
            latency_ms=latency.get("max", 0.0),
            latency_rolling_mean_ms=latency.get("avg", 0.0),
            error_rate=errors.get("avg", 0.0),
            sample_count=sample_count,
            latency_rolling_std_ms=latency.get("std", 0.0),
        ))
    return features


# ── publish gate ──────────────────────────────────────────────────────────────

def should_publish(score: AnomalyScore, policy: EnginePolicy) -> bool:
    """Decide whether a score becomes a published AnomalyEvent.

    Rules:
      - safe_mode=true               → never publish (operators have explicitly
                                       paused decision flow)
      - anomaly_response="advisory"  → publish every score (downstream callers
                                       see the signal but won't auto-isolate)
      - default ("auto-isolate")     → publish only non-healthy scores so
                                       healthy-noise doesn't flood the bus
    """
    if policy.safe_mode:
        return False
    if policy.anomaly_response == "advisory":
        return True
    return score.status != "healthy"


# ── stability gate (per-backend memory across cycles) ───────────────────────

@dataclass
class BackendState:
    """Per-backend memory carried across inference cycles by
    apply_stability_gate(). One instance per backend_id, owned by app.py's
    run loop (not thread-shared)."""
    last_status: str = "healthy"
    last_score: float = 0.0
    pending_status: str | None = None
    pending_count: int = 0
    # Consecutive cycles the low-sample hold (B1) has frozen a non-healthy
    # status. Used by the optional max_hold_cycles TTL so a backend that goes
    # permanently quiet can't be pinned non-healthy forever.
    low_sample_hold_count: int = 0


def apply_stability_gate(
    raw: AnomalyScore,
    low_sample: bool,
    state: BackendState,
    confirmation_cycles: int,
    max_hold_cycles: int | None = None,
) -> AnomalyScore:
    """Wrap an engine's raw AnomalyScore with per-backend memory.

    Fixes two operational gaps:

    - B1 (sample-count blind spot): engines force "healthy"/0.0 when
      features.sample_count < min_sample_count (see
      IsolationForestEngine.score / ThresholdEngine.score) -- a backend
      failing fast on every request produces few samples and would
      otherwise be reported healthy. "No new evidence" should mean "no
      change": if `low_sample` is True and the backend's last confirmed
      status was non-healthy, that status/score is preserved instead.

    - B2 (no hysteresis/cooldown): a status change away from
      `state.last_status` must be observed for `confirmation_cycles`
      consecutive cycles before it is confirmed (returned as-is). Until
      confirmed, the previous stable status/score is returned, so a single
      noisy sample can't flip a backend's published status.

    `max_hold_cycles` (TTL) bounds the B1 hold: without it, a backend that
    goes permanently quiet (low_sample forever) after a non-healthy reading
    would be pinned non-healthy until process restart. When the hold has run
    for more than `max_hold_cycles` consecutive cycles, the hold is released
    and the raw (low-sample) reading is processed normally — so the status
    decays back toward healthy through the usual confirmation path instead of
    sticking forever. `None` (default) preserves the original unbounded hold.

    Mutates `state` in place and returns the gated AnomalyScore. Evidence
    fields (metric / observed_value / threshold) are carried from `raw` only
    when the raw verdict is the one returned; a held/pending verdict reuses
    the last confirmed status with no stale evidence attached.
    """
    if low_sample and state.last_status != "healthy":
        state.low_sample_hold_count += 1
        if max_hold_cycles is None or state.low_sample_hold_count <= max_hold_cycles:
            return AnomalyScore(raw.backend_id, state.last_status, state.last_score)
        # TTL exceeded: fall through and let the raw reading be processed by the
        # normal confirmation logic below, so the held status can decay.
    else:
        state.low_sample_hold_count = 0

    if raw.status == state.last_status:
        state.last_score = raw.score
        state.pending_status = None
        state.pending_count = 0
        return raw

    if state.pending_status == raw.status:
        state.pending_count += 1
    else:
        state.pending_status = raw.status
        state.pending_count = 1

    if state.pending_count >= confirmation_cycles:
        state.last_status = raw.status
        state.last_score = raw.score
        state.pending_status = None
        state.pending_count = 0
        return raw

    return AnomalyScore(raw.backend_id, state.last_status, state.last_score)


def _severity_for_status(status: str) -> str | None:
    """Map the three-tier health status onto the operator-ui alert bucket.
    healthy verdicts aren't alerts, so they get no severity."""
    if status == "unhealthy":
        return "critical"
    if status == "degraded":
        return "warning"
    return None


def score_to_event_payload(score: AnomalyScore, model_version: str) -> dict:
    """Serialise an AnomalyScore + model id into the AnomalyEvent dict shape
    expected by publish_envelope. model_version goes onto the envelope for
    debug / provenance per the contracts.py docstring.

    When the engine attached evidence (metric / observed_value / threshold) the
    payload carries it through plus a derived UI severity, so the operator-ui
    Active Alerts panel can render "latency_ms 312 (threshold 250)" without a
    second round-trip. Evidence keys are omitted when absent to keep healthy /
    legacy payloads unchanged."""
    payload = {
        "backend_id":    score.backend_id,
        "status":        score.status,
        "score":         score.score,
        "model_version": model_version,
    }
    if score.metric is not None:
        payload["metric"] = score.metric
    if score.observed_value is not None:
        payload["observed_value"] = score.observed_value
    if score.threshold is not None:
        payload["threshold"] = score.threshold
    severity = _severity_for_status(score.status)
    if severity is not None:
        payload["severity"] = severity
    return payload


# ── /api/v1/engine/state serialisation ───────────────────────────────────────

def serialize_engine_state(
    *,
    service: str,
    channel: str,
    runloop_enabled: bool,
    engine_name: str,
    engine_requested: str,
    engine_ready: bool,
    engine_error: str | None,
    policy: EnginePolicy,
    ticks_total: int,
    publishes_total: int,
    last_tick_at: str | None,
    last_publish_at: str | None,
    last_tick_monotonic: float | None,
    last_output: list[dict] | dict | None,
) -> dict:
    """Build the /api/v1/engine/state response dict for the Live Engines page.

    Pure-Python so it can be unit-tested without Flask. The caller (app.py)
    snapshots all runloop globals under _state_lock and passes them here.
    """
    last_tick_age = (
        None if last_tick_monotonic is None
        else round(time.monotonic() - last_tick_monotonic, 2)
    )
    return {
        "service": service,
        "channel": channel,
        "runloop_enabled": runloop_enabled,
        "engine": {
            "kind": "engine",
            "requested": engine_requested,
            "loaded": engine_name,
            "ready": engine_ready,
            "error": engine_error,
        },
        "policy_snapshot": asdict(policy),
        "stats": {
            "ticks_total": ticks_total,
            "publishes_total": publishes_total,
            "last_tick_at": last_tick_at,
            "last_publish_at": last_publish_at,
            "last_tick_age_seconds": last_tick_age,
        },
        "last_output": last_output,
    }
