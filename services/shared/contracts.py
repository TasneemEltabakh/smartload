"""
services/shared/contracts.py
────────────────────────────
Typed dataclasses for all Redis control-bus messages in SmartLoad, plus the
canonical Envelope wrapper and publish/subscribe helpers.

Every service that publishes or subscribes to the Redis control bus MUST use
these classes for serialisation/deserialisation. This guarantees the message
schema is consistent across the entire pipeline.

Redis channels:
  smartload.anomaly   ← AnomalyEvent          (published by anomaly-detector)
  smartload.forecast  ← ForecastResult        (published by forecasting)
  smartload.routing   ← RoutingRecommendation (published by rl-engine)
  smartload.policy    ← PolicyUpdate          (published by policy-manager)
  smartload.scale     ← ScalingEvent          (published by autoscaler)

Usage (new envelope-aware path):
    from services.shared.contracts import (
        AnomalyEvent, publish_envelope, subscribe_envelope,
    )

    # Publish — envelope fields are filled automatically.
    publish_envelope(
        redis_client,
        channel="smartload.anomaly",
        source="anomaly-detector",
        payload=AnomalyEvent(backend_id="backend_1", status="unhealthy", score=0.92),
    )

    # Subscribe — stale messages are dropped per channel TTL.
    def on_event(payload: dict, envelope: dict) -> None:
        ...
    subscribe_envelope(pubsub, channel="smartload.anomaly", callback=on_event)

Usage (legacy flat path — kept for the existing 33-test integration suite):
    from services.shared.contracts import AnomalyEvent, json_encode, json_decode
    redis_client.publish("smartload.anomaly", json_encode(event))
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from typing import Any, Callable


# ── helpers ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_dict(obj: Any) -> dict:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return obj
    raise TypeError(f"cannot serialise {type(obj).__name__}; pass a dataclass or dict")


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


# ── canonical envelope (SOT §11) ──────────────────────────────────────────────

ENVELOPE_VERSION = 1

# Channel-specific staleness TTLs (seconds). Subscribers MUST drop messages
# older than the TTL for their channel. Defaults from SOT §11 envelope rules.
CHANNEL_TTL_SECONDS: dict[str, int | None] = {
    "smartload.anomaly":  30,
    "smartload.routing":  30,
    "smartload.forecast": 180,
    "smartload.scale":    None,   # actions are auditable; never expire
    "smartload.policy":   None,   # policy snapshots never expire
}


@dataclass
class Envelope:
    """
    Canonical wrapper for every Redis pub/sub message.

    Every channel carries the same envelope fields; payload-specific fields
    (anomaly score, predicted RPS, etc.) live inside `payload`.

    SOT §11 envelope rules:
      - event_id enables subscriber dedup across re-publishes.
      - source identifies the publisher (kebab-case service name).
      - version is the envelope schema version, bumped only on envelope
        semantics changes — independent of channel suffix.
      - timestamp is RFC 3339 UTC; subscribers reject messages older than
        the channel TTL.
      - payload holds the typed dataclass body.
    """
    event_id:  str
    source:    str
    version:   int
    timestamp: str
    payload:   dict


def make_envelope(source: str, payload: Any) -> Envelope:
    """Build an Envelope around `payload`, filling event_id and timestamp."""
    return Envelope(
        event_id=str(uuid.uuid4()),
        source=source,
        version=ENVELOPE_VERSION,
        timestamp=_now_iso(),
        payload=_to_dict(payload),
    )


def publish_envelope(redis_client, channel: str, source: str, payload: Any) -> str:
    """
    Wrap `payload` in a canonical Envelope and publish it on `channel`.

    Returns the event_id of the published message (useful for tests and for
    correlating audit log entries).

    Subscribers should use `subscribe_envelope` to receive these — that helper
    drops stale messages and unwraps the envelope.
    """
    env = make_envelope(source=source, payload=payload)
    redis_client.publish(channel, json.dumps(asdict(env)))
    return env.event_id


def _envelope_is_stale(env_timestamp: str, ttl_seconds: int | None) -> bool:
    if ttl_seconds is None:
        return False
    try:
        ts = datetime.fromisoformat(env_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return True   # unparseable timestamp ⇒ treat as stale, refuse silently
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age > ttl_seconds


def parse_envelope(raw: str | bytes, channel: str | None = None) -> tuple[dict, dict] | None:
    """
    Parse a JSON envelope from a Redis message body.

    Returns (payload, envelope_meta) on success, or None if the message is
    malformed or stale per the channel TTL.

    `envelope_meta` excludes `payload` so callers can correlate by event_id /
    source / version without re-reading the full body.
    """
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or "payload" not in data or "timestamp" not in data:
        return None
    ttl = CHANNEL_TTL_SECONDS.get(channel) if channel else None
    if _envelope_is_stale(data["timestamp"], ttl):
        return None
    payload = data.pop("payload")
    return payload, data


def subscribe_envelope(
    pubsub,
    channel: str,
    callback: Callable[[dict, dict], None],
    *,
    timeout: float | None = None,
) -> None:
    """
    Pull one message from `pubsub` (already SUBSCRIBE'd to `channel`), parse
    its envelope, and invoke `callback(payload, envelope_meta)` if the message
    is valid and not stale.

    Returns silently on timeout, malformed body, or a stale message — never
    raises. Caller drives the polling loop; this helper is a single-tick.
    """
    msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
    if msg is None or msg.get("type") != "message":
        return
    parsed = parse_envelope(msg.get("data", b""), channel=channel)
    if parsed is None:
        return
    payload, meta = parsed
    callback(payload, meta)


# ── message contracts ─────────────────────────────────────────────────────────

@dataclass
class AnomalyEvent:
    """
    Published by anomaly-detector to smartload.anomaly.
    Consumed by: load-balancer sidecar (T2.1), policy-manager.

    SOT §11 channel-specific payload fields:
      required: backend_id, status, score
      optional: features (debug map), model_version
    """
    backend_id: str
    status: str        # "healthy" | "degraded" | "unhealthy"
    score: float       # anomaly score — higher means more anomalous
    timestamp: str = field(default_factory=_now_iso)
    # ── optional debug / provenance ──────────────────────────────────────────
    features: dict | None = None        # per-feature contributions (debug only)
    model_version: str | None = None    # which model produced the score


@dataclass
class ForecastResult:
    """
    Published by forecasting to smartload.forecast.
    Consumed by: autoscaler, policy-manager.

    SOT §11 channel-specific payload fields:
      required: horizon_minutes, predicted_rps
      optional: confidence_lower, confidence_upper, model_id
    """
    horizon_minutes: int        # look-ahead window (e.g. 5)
    predicted_rps: float        # predicted requests-per-second
    confidence_lower: float     # lower bound of prediction interval
    confidence_upper: float     # upper bound of prediction interval
    timestamp: str = field(default_factory=_now_iso)
    # ── optional provenance ──────────────────────────────────────────────────
    model_id: str | None = None         # e.g. "moving_average" | "arima_v2"


@dataclass
class RoutingRecommendation:
    """
    Published by rl-engine to smartload.routing.
    Consumed by: load-balancer sidecar (T2.1).
    mode="shadow"  → log only, do not affect live routing
    mode="active"  → load balancer applies these weights

    SOT §11 channel-specific payload fields:
      required: mode, server_rankings:[{backend_id, score}]
      optional: policy_version (the policy snapshot RL was reasoning under)
    """
    mode: str                       # "shadow" | "active"
    server_rankings: list[dict]     # [{"backend_id": str, "score": float}, ...]
    timestamp: str = field(default_factory=_now_iso)
    # ── optional provenance ──────────────────────────────────────────────────
    policy_version: int | None = None   # join key back to PolicyUpdate.policy_version


@dataclass
class ScalingEvent:
    """
    Published by autoscaler to smartload.scale.
    Consumed by: policy-manager (for audit), load-balancer (to update pool size).

    SOT §11 channel-specific payload fields:
      required: action, instance_count, reason
      optional: forecast_event_id (provenance — which forecast triggered this)
    """
    action: str          # "scale_out" | "scale_in"
    instance_count: int  # resulting backend count after action
    reason: str          # human-readable justification
    timestamp: str = field(default_factory=_now_iso)
    # ── optional provenance ──────────────────────────────────────────────────
    forecast_event_id: str | None = None    # join key back to ForecastResult


@dataclass
class PolicyUpdate:
    """
    Published by policy-manager to smartload.policy whenever a field changes.
    Consumed by: all AI services that read policy (anomaly-detector,
                 forecasting, rl-engine, autoscaler) and the LB sidecar.

    The full canonical field set lives in config/policy.yaml. New optional
    fields can be added without bumping the envelope version (additive change
    is non-breaking — SOT §11).

    SOT §11 channel-specific payload fields:
      required: full snapshot of all canonical fields, policy_version (monotonic)
      optional: changed_fields (list of field names differing from previous)
    """
    operating_mode: str
    safe_mode: bool
    min_backends: int
    max_backends: int
    slo_p95_latency_ms: int
    anomaly_latency_multiplier: float
    per_instance_capacity_rps: int
    autoscaler_cooldown_seconds: int
    policy_version: int             # monotonic int — Policy Manager increments per change
    # ── added in S2 baseline (SOT §8.9 canonical fields) ──────────────────────
    anomaly_response: str = "auto-isolate"           # "auto-isolate" | "advisory"
    anomaly_recovery_window_seconds: int = 30
    rl_exploration_rate: float = 0.0
    rl_confidence_threshold: float = 0.6
    # ── optional metadata ────────────────────────────────────────────────────
    changed_fields: list[str] | None = None
    timestamp: str = field(default_factory=_now_iso)
