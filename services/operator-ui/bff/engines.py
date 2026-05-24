"""
services/operator-ui/bff/engines.py
────────────────────────────────────
Live Engines (#121) — Redis subscriber + per-channel ring buffer + per-client
event bus.

The BFF subscribes once to smartload.{anomaly,forecast,routing,scale} on a
background thread. Every parsed envelope is appended to a per-channel deque
(capacity 100) and broadcast to every SSE subscriber. The Flask route handlers
read from the deques (/api/ui/engines/snapshot is point-in-time) or subscribe
to the bus (/api/ui/engines/stream is live).

Pure-Python except for the redis dependency in `subscriber_loop`. Everything
else can be unit-tested without Redis or Flask.

Per SOT §28 (operator-ui): pure read, owns no channel or table. This module
preserves that — it only reads what the AI services publish.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from collections import deque
from typing import Callable, Iterable

# Make services/shared/ importable so we can use the canonical parse_envelope.
# Same defensive pattern as the AI services — works in /app (container) and
# in dev (services/operator-ui/bff/).
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (_HERE, os.path.dirname(_HERE), os.path.dirname(os.path.dirname(_HERE))):
    if os.path.isdir(os.path.join(_cand, "shared")):
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
        break

from shared.contracts import parse_envelope  # noqa: E402


# ── topology ─────────────────────────────────────────────────────────────────

CHANNELS: tuple[str, ...] = (
    "smartload.anomaly",
    "smartload.forecast",
    "smartload.routing",
    "smartload.scale",
)

RING_CAPACITY = 100
QUEUE_CAPACITY = 256          # per-subscriber max buffered events
HEARTBEAT_INTERVAL_SECONDS = 15.0


# ── ring buffer (per-channel) ────────────────────────────────────────────────

class RingBuffer:
    """Per-channel deque of recent envelope entries.

    Thread-safe. Bounded by RING_CAPACITY per channel — older entries fall off
    the back. Stores fully-parsed entries as plain dicts so the Flask handlers
    can serialise them directly without re-touching the envelope helpers.
    """

    def __init__(self, capacity: int = RING_CAPACITY,
                 channels: Iterable[str] = CHANNELS) -> None:
        self._buf: dict[str, deque[dict]] = {
            ch: deque(maxlen=capacity) for ch in channels
        }
        self._lock = threading.Lock()

    def append(self, channel: str, entry: dict) -> bool:
        """Append entry to the channel's deque. Returns True on success,
        False if the channel isn't tracked (defensive — protects against
        a publisher emitting on a channel we never subscribed to)."""
        with self._lock:
            d = self._buf.get(channel)
            if d is None:
                return False
            d.append(entry)
            return True

    def snapshot(self) -> dict[str, list[dict]]:
        """Return a shallow copy of every channel's deque, newest-last.
        Safe to serialise; the returned lists are independent of internal
        state."""
        with self._lock:
            return {ch: list(d) for ch, d in self._buf.items()}

    def recent(self, limit: int | None = None) -> list[dict]:
        """Return entries from all channels merged, sorted by envelope
        timestamp ascending. Used to bootstrap a new SSE connection so the
        operator sees recent activity, not a blank feed."""
        with self._lock:
            merged: list[dict] = []
            for d in self._buf.values():
                merged.extend(d)
        merged.sort(key=lambda e: e.get("envelope", {}).get("timestamp", ""))
        if limit is not None and limit >= 0:
            return merged[-limit:]
        return merged


# ── event bus (per-subscriber) ───────────────────────────────────────────────

class EngineEventBus:
    """Fan-out to N SSE subscribers. Each subscriber gets its own bounded
    queue; a slow subscriber drops events rather than blocking publishers.

    The Redis subscriber thread calls `broadcast` on every parsed entry. Each
    SSE handler calls `subscribe` to get its own queue, polls it, then
    `unsubscribe`s on disconnect.
    """

    def __init__(self) -> None:
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self, capacity: int = QUEUE_CAPACITY) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=capacity)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def broadcast(self, entry: dict) -> int:
        """Push entry to every subscriber's queue. Returns the number of
        deliveries (subscribers minus drops). Slow subscribers whose queues
        are full silently drop this event — they'll catch up on the next
        one, and SSE clients have the ring buffer for backfill."""
        delivered = 0
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(entry)
                delivered += 1
            except queue.Full:
                pass
        return delivered

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


# ── parse one Redis message into a ring-buffer entry ─────────────────────────

def build_entry(channel: str, raw_message: bytes | str) -> dict | None:
    """Parse a Redis message body into a ring-buffer entry, or return None if
    the envelope is malformed / stale per parse_envelope's rules.

    The returned entry is the canonical shape every API surface uses:
        {
          "channel":  "smartload.<topic>",
          "envelope": {event_id, source, version, timestamp},
          "payload":  {channel-specific fields...},
        }
    """
    parsed = parse_envelope(raw_message, channel=channel)
    if parsed is None:
        return None
    payload, envelope_meta = parsed
    return {
        "channel": channel,
        "envelope": envelope_meta,
        "payload": payload,
    }


# ── subscriber loop ──────────────────────────────────────────────────────────

def subscriber_loop(
    redis_client_factory: Callable[[], object],
    buf: RingBuffer,
    bus: EngineEventBus,
    *,
    channels: Iterable[str] = CHANNELS,
    stop_event: threading.Event | None = None,
    reconnect_delay_seconds: float = 2.0,
    log: Callable[[str], None] | None = None,
) -> None:
    """Subscribe to every channel, parse each message, append to buf, broadcast
    to bus. Reconnects on Redis failure with a small backoff.

    redis_client_factory is a no-arg callable returning a redis.Redis instance
    — passed instead of a URL so tests can inject a fake client without
    importing the redis library.
    """
    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    channel_list = list(channels)

    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            client = redis_client_factory()
            pubsub = client.pubsub()
            pubsub.subscribe(*channel_list)
            _log(f"engines-subscriber: subscribed to {channel_list}")
            for msg in pubsub.listen():
                if stop_event is not None and stop_event.is_set():
                    return
                if msg.get("type") != "message":
                    continue
                channel = msg.get("channel")
                if isinstance(channel, bytes):
                    channel = channel.decode()
                entry = build_entry(channel, msg.get("data", b""))
                if entry is None:
                    continue
                buf.append(channel, entry)
                bus.broadcast(entry)
        except Exception as exc:                            # noqa: BLE001
            _log(f"engines-subscriber: error {exc!r}; reconnecting in "
                 f"{reconnect_delay_seconds}s")
            if stop_event is not None and stop_event.wait(reconnect_delay_seconds):
                return
            else:
                time.sleep(reconnect_delay_seconds)


# ── SSE frame helpers (pure — testable without Flask) ────────────────────────

def format_sse_event(entry: dict, encoder: Callable[[dict], str]) -> str:
    """Format a ring-buffer entry as a single SSE 'data:' frame.

    encoder is injected (json.dumps in production) so tests can use a stable
    serialiser without depending on key ordering.
    """
    return f"data: {encoder(entry)}\n\n"


def format_sse_heartbeat() -> str:
    """SSE comment-only frame. Keeps idle connections alive through proxies
    that close TCP after N seconds of silence."""
    return ": heartbeat\n\n"


# ── default engine names (for snapshot fan-out) ──────────────────────────────

# (service-name, default-base-url). The BFF reads the URL from environment
# variables matching the AI service it talks to — same pattern as SERVICE_URLS
# in app.py for /api/ui/health.
ENGINE_SERVICES: tuple[tuple[str, str, str], ...] = (
    ("anomaly-detector", "ANOMALY_DETECTOR_URL", "http://anomaly-detector:8082"),
    ("forecasting",      "FORECASTING_URL",      "http://forecasting:8083"),
    ("rl-engine",        "RL_ENGINE_URL",        "http://rl-engine:8084"),
)
