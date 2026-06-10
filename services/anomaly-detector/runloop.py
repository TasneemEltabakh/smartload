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

# ANOMALY_QUERY shape: one row per (instance, metric_name) pair, with columns
#   (instance, metric_name, avg_value, max_value, std_value, sample_count)
# We pivot it into one BackendFeatures per instance.

def build_features_from_rows(rows: list[tuple]) -> list[BackendFeatures]:
    """Pivot ANOMALY_QUERY rows into one BackendFeatures per backend.

    Rows are tuples: (instance, metric_name, avg, max, std, sample_count).
    Returns an empty list when no rows arrive (cold DB, idle stack).
    """
    by_instance: dict[str, dict] = {}
    for row in rows:
        try:
            instance, metric_name, avg, mx, std, samples = row
        except ValueError:
            continue   # malformed row — skip, don't poison the whole batch
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


def score_to_event_payload(score: AnomalyScore, model_version: str) -> dict:
    """Serialise an AnomalyScore + model id into the AnomalyEvent dict shape
    expected by publish_envelope. model_version goes onto the envelope for
    debug / provenance per the contracts.py docstring."""
    return {
        "backend_id":    score.backend_id,
        "status":        score.status,
        "score":         score.score,
        "model_version": model_version,
    }


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
