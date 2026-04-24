"""
services/shared/contracts.py
────────────────────────────
Typed dataclasses for all Redis control-bus messages in SmartLoad.

Every service that publishes or subscribes to the Redis control bus MUST use
these classes for serialisation/deserialisation. This guarantees that the
message schema is consistent across the entire pipeline.

Redis channels:
  smartload.anomaly   ← AnomalyEvent        (published by anomaly-detector)
  smartload.forecast  ← ForecastResult      (published by forecasting)
  smartload.routing   ← RoutingRecommendation (published by rl-engine)
  smartload.policy    ← dict / raw JSON     (published by policy-manager)
  smartload.scale     ← ScalingEvent        (published by autoscaler)

Usage:
    from services.shared.contracts import AnomalyEvent, json_encode, json_decode

    # Publish
    event = AnomalyEvent(backend_id="backend_1", status="unhealthy", score=0.92,
                         timestamp=datetime.utcnow().isoformat() + "Z")
    redis_client.publish("smartload.anomaly", json_encode(event))

    # Subscribe
    raw = pubsub.get_message()
    event = json_decode(raw["data"], AnomalyEvent)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


# ── helpers ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_encode(obj: Any) -> str:
    """Serialise a dataclass (or plain dict) to a JSON string for Redis publish."""
    if hasattr(obj, "__dataclass_fields__"):
        return json.dumps(asdict(obj))
    return json.dumps(obj)


def json_decode(data: str | bytes, cls: type) -> Any:
    """Deserialise a JSON string from Redis into a dataclass instance."""
    if isinstance(data, bytes):
        data = data.decode()
    d = json.loads(data)
    return cls(**d)


# ── message contracts ─────────────────────────────────────────────────────────

@dataclass
class AnomalyEvent:
    """
    Published by anomaly-detector to smartload.anomaly.
    Consumed by: load-balancer sidecar (T2.1), policy-manager.
    """
    backend_id: str
    status: str        # "healthy" | "degraded" | "unhealthy"
    score: float       # anomaly score — higher means more anomalous
    timestamp: str = field(default_factory=_now_iso)


@dataclass
class ForecastResult:
    """
    Published by forecasting to smartload.forecast.
    Consumed by: autoscaler, policy-manager.
    """
    horizon_minutes: int        # look-ahead window (e.g. 5)
    predicted_rps: float        # predicted requests-per-second
    confidence_lower: float     # lower bound of prediction interval
    confidence_upper: float     # upper bound of prediction interval
    timestamp: str = field(default_factory=_now_iso)


@dataclass
class RoutingRecommendation:
    """
    Published by rl-engine to smartload.routing.
    Consumed by: load-balancer sidecar (T2.1).
    mode="shadow"  → log only, do not affect live routing
    mode="active"  → load balancer applies these weights
    """
    mode: str                       # "shadow" | "active"
    server_rankings: list[dict]     # [{"backend_id": str, "score": float}, ...]
    timestamp: str = field(default_factory=_now_iso)


@dataclass
class ScalingEvent:
    """
    Published by autoscaler to smartload.scale.
    Consumed by: policy-manager (for audit), load-balancer (to update pool size).
    """
    action: str          # "scale_out" | "scale_in"
    instance_count: int  # resulting backend count after action
    reason: str          # human-readable justification
    timestamp: str = field(default_factory=_now_iso)


@dataclass
class PolicyUpdate:
    """
    Published by policy-manager to smartload.policy whenever a field changes.
    Consumed by: all AI services that read policy (anomaly-detector, rl-engine, autoscaler).
    """
    operating_mode: str
    safe_mode: bool
    min_backends: int
    max_backends: int
    slo_p95_latency_ms: int
    anomaly_latency_multiplier: float
    per_instance_capacity_rps: int
    autoscaler_cooldown_seconds: int
    timestamp: str = field(default_factory=_now_iso)
