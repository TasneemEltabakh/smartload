"""
clients/python/smartload_client/_envelope.py
─────────────────────────────────────────────
Minimal envelope parser for the SmartLoad Redis control bus.

Mirrors `services/shared/contracts.py::parse_envelope` *without* importing
it — the SDK is a separately-installable package and must not depend on
the monorepo `services/` tree. The wire shape is stable per SOT §11; if it
ever changes, this file and `contracts.py` move together.

Stale-message handling: subscribers MUST drop messages older than the
per-channel TTL (SOT §11). For policy / scale channels TTL is unbounded
(both are auditable). For anomaly / routing / forecast there is a TTL.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

CHANNEL_POLICY    = "smartload.policy"
CHANNEL_ANOMALY   = "smartload.anomaly"
CHANNEL_FORECAST  = "smartload.forecast"
CHANNEL_ROUTING   = "smartload.routing"
CHANNEL_SCALE     = "smartload.scale"

_CHANNEL_TTL_SECONDS: dict[str, int | None] = {
    CHANNEL_ANOMALY:  30,
    CHANNEL_ROUTING:  30,
    CHANNEL_FORECAST: 180,
    CHANNEL_SCALE:    None,
    CHANNEL_POLICY:   None,
}


def parse_envelope(raw, channel: str | None = None) -> tuple[dict, dict] | None:
    """Decode a Redis pub/sub message body into (payload, envelope_meta).

    Returns None if the message is malformed or stale per the channel TTL.
    Never raises.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode()
        except UnicodeDecodeError:
            return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or "payload" not in data or "timestamp" not in data:
        return None

    ttl = _CHANNEL_TTL_SECONDS.get(channel) if channel else None
    if ttl is not None:
        try:
            ts = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
        if ts.tzinfo is None:
            return None
        if (datetime.now(timezone.utc) - ts).total_seconds() > ttl:
            return None

    payload = data.pop("payload")
    return payload, data
