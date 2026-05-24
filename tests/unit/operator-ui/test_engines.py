"""
tests/unit/operator-ui/test_engines.py
───────────────────────────────────────
Pure-Python unit tests for services/operator-ui/bff/engines.py (Live Engines
ring buffer + event bus + subscriber loop + SSE helpers).

No Docker, no Redis, no Flask — runs in the unit-tests CI job.

Coverage:
  1. RingBuffer — capacity, snapshot independence, recent ordering,
                  rejection of unknown channels.
  2. EngineEventBus — broadcast deliveries, slow-subscriber drop, unsubscribe
                      idempotency.
  3. build_entry — happy path, malformed-json drop, not-an-envelope drop.
  4. format_sse_event / format_sse_heartbeat — frame shape.
  5. subscriber_loop — drives a fake pubsub, asserts the loop appends to the
                       ring and broadcasts to subscribers; stop_event exits;
                       reconnects after a raised exception.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Add services/operator-ui/bff/ to sys.path so we can import engines + use
# shared.contracts (engines.py sets that up itself, but we need bff/ too).
_BFF = Path(__file__).resolve().parents[2].parent / "services" / "operator-ui" / "bff"
if str(_BFF) not in sys.path:
    sys.path.insert(0, str(_BFF))

from engines import (                                       # noqa: E402
    CHANNELS,
    EngineEventBus,
    RingBuffer,
    build_entry,
    format_sse_event,
    format_sse_heartbeat,
    subscriber_loop,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _envelope_bytes(channel: str, payload: dict, *, source: str = "unit-test") -> bytes:
    """Build the JSON envelope bytes parse_envelope would accept."""
    body = {
        "event_id":  str(uuid.uuid4()),
        "source":    source,
        "version":   1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload":   payload,
    }
    return json.dumps(body).encode()


# ── RingBuffer ───────────────────────────────────────────────────────────────

def test_ring_buffer_appends_under_channel():
    buf = RingBuffer(capacity=5)
    assert buf.append("smartload.anomaly", {"channel": "smartload.anomaly", "envelope": {}, "payload": {"x": 1}}) is True
    snap = buf.snapshot()
    assert len(snap["smartload.anomaly"]) == 1
    assert snap["smartload.anomaly"][0]["payload"] == {"x": 1}


def test_ring_buffer_rejects_unknown_channel():
    buf = RingBuffer(capacity=5)
    ok = buf.append("smartload.invented", {"payload": {}})
    assert ok is False


def test_ring_buffer_capacity_enforced():
    buf = RingBuffer(capacity=3)
    for i in range(10):
        buf.append("smartload.forecast", {"payload": {"i": i}})
    snap = buf.snapshot()
    assert len(snap["smartload.forecast"]) == 3
    # Newest entries retained, oldest dropped.
    assert [e["payload"]["i"] for e in snap["smartload.forecast"]] == [7, 8, 9]


def test_ring_buffer_snapshot_is_independent_copy():
    buf = RingBuffer()
    buf.append("smartload.anomaly", {"payload": {}})
    snap = buf.snapshot()
    snap["smartload.anomaly"].clear()                       # mutate the copy
    # Internal state untouched.
    assert len(buf.snapshot()["smartload.anomaly"]) == 1


def test_ring_buffer_recent_sorts_across_channels_by_timestamp():
    buf = RingBuffer()
    buf.append("smartload.anomaly", {
        "channel": "smartload.anomaly",
        "envelope": {"timestamp": "2026-05-24T10:00:00+00:00"},
        "payload": {"i": 1},
    })
    buf.append("smartload.forecast", {
        "channel": "smartload.forecast",
        "envelope": {"timestamp": "2026-05-24T09:00:00+00:00"},
        "payload": {"i": 2},
    })
    buf.append("smartload.routing", {
        "channel": "smartload.routing",
        "envelope": {"timestamp": "2026-05-24T11:00:00+00:00"},
        "payload": {"i": 3},
    })
    recent = buf.recent()
    timestamps = [e["envelope"]["timestamp"] for e in recent]
    assert timestamps == sorted(timestamps)


def test_ring_buffer_recent_with_limit_returns_tail():
    buf = RingBuffer()
    for i in range(20):
        buf.append("smartload.forecast", {
            "channel": "smartload.forecast",
            "envelope": {"timestamp": f"2026-05-24T10:00:{i:02d}+00:00"},
            "payload": {"i": i},
        })
    recent = buf.recent(limit=5)
    assert len(recent) == 5
    assert [e["payload"]["i"] for e in recent] == [15, 16, 17, 18, 19]


# ── EngineEventBus ───────────────────────────────────────────────────────────

def test_bus_broadcast_delivers_to_every_subscriber():
    bus = EngineEventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    delivered = bus.broadcast({"x": 1})
    assert delivered == 2
    assert q1.get_nowait() == {"x": 1}
    assert q2.get_nowait() == {"x": 1}


def test_bus_unsubscribe_stops_delivery():
    bus = EngineEventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    bus.unsubscribe(q1)
    delivered = bus.broadcast({"x": 1})
    assert delivered == 1
    assert q2.get_nowait() == {"x": 1}
    assert q1.empty()


def test_bus_unsubscribe_unknown_queue_is_idempotent():
    bus = EngineEventBus()
    stray: queue.Queue = queue.Queue()
    # Must not raise even though `stray` was never subscribed.
    bus.unsubscribe(stray)


def test_bus_slow_subscriber_drops_silently():
    bus = EngineEventBus()
    q = bus.subscribe(capacity=2)
    # Fill the queue.
    bus.broadcast({"i": 0})
    bus.broadcast({"i": 1})
    # Third broadcast should be dropped for this subscriber (queue full)
    # but the broadcast itself must not raise.
    delivered = bus.broadcast({"i": 2})
    assert delivered == 0
    # First two still readable.
    assert q.get_nowait()["i"] == 0
    assert q.get_nowait()["i"] == 1


def test_bus_subscriber_count_reflects_lifecycle():
    bus = EngineEventBus()
    assert bus.subscriber_count() == 0
    q = bus.subscribe()
    assert bus.subscriber_count() == 1
    bus.unsubscribe(q)
    assert bus.subscriber_count() == 0


# ── build_entry ──────────────────────────────────────────────────────────────

def test_build_entry_parses_valid_envelope():
    raw = _envelope_bytes("smartload.anomaly", {"backend_id": "b1", "status": "degraded"})
    entry = build_entry("smartload.anomaly", raw)
    assert entry is not None
    assert entry["channel"] == "smartload.anomaly"
    assert entry["payload"] == {"backend_id": "b1", "status": "degraded"}
    assert entry["envelope"]["source"] == "unit-test"
    assert entry["envelope"]["version"] == 1
    assert "event_id" in entry["envelope"]
    assert "timestamp" in entry["envelope"]


def test_build_entry_drops_malformed_json():
    assert build_entry("smartload.anomaly", b"{not json") is None


def test_build_entry_drops_envelope_without_payload_key():
    bad = json.dumps({"timestamp": datetime.now(timezone.utc).isoformat()}).encode()
    assert build_entry("smartload.anomaly", bad) is None


# ── SSE frame helpers ────────────────────────────────────────────────────────

def test_format_sse_event_uses_provided_encoder():
    entry = {"channel": "smartload.forecast", "payload": {"predicted_rps": 12.5}}
    frame = format_sse_event(entry, json.dumps)
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    body = frame[len("data: "):-2]
    assert json.loads(body) == entry


def test_format_sse_heartbeat_is_comment_frame():
    frame = format_sse_heartbeat()
    assert frame.startswith(":")
    assert frame.endswith("\n\n")
    # Not a 'data:' frame — clients silently ignore comments.
    assert "data:" not in frame


# ── subscriber_loop (driven by a fake pubsub) ────────────────────────────────

class _FakePubsub:
    """Minimal stand-in for redis.client.PubSub. The runtime calls
    .subscribe(*channels) once and iterates over .listen(). A real listen()
    blocks indefinitely after delivering — we model that with a stop_event
    so the subscriber_loop's outer `while True` doesn't immediately
    re-subscribe and re-deliver the same messages."""

    def __init__(self, messages: list[dict], block_event: threading.Event):
        self._messages = list(messages)
        self._block_event = block_event
        self.subscribed_to: list[str] = []

    def subscribe(self, *channels: str) -> None:
        self.subscribed_to.extend(channels)

    def listen(self):
        for m in self._messages:
            yield m
        # Block until told to stop — mimics a real pubsub.listen() that
        # blocks on the socket. Without this the loop would re-subscribe.
        self._block_event.wait()


class _FakeRedisClient:
    def __init__(self, messages: list[dict], block_event: threading.Event):
        self._messages = messages
        self._block_event = block_event

    def pubsub(self) -> _FakePubsub:
        return _FakePubsub(self._messages, self._block_event)


def test_subscriber_loop_appends_and_broadcasts_valid_envelopes():
    raw_anom = _envelope_bytes("smartload.anomaly", {"backend_id": "b1", "status": "unhealthy"})
    messages = [
        {"type": "subscribe", "channel": b"smartload.anomaly"},   # ignored
        {"type": "message",   "channel": b"smartload.anomaly", "data": raw_anom},
    ]
    buf = RingBuffer()
    bus = EngineEventBus()
    q = bus.subscribe()
    stop = threading.Event()
    block = threading.Event()

    def factory():
        return _FakeRedisClient(messages, block)

    # The fake's listen() blocks on `block` after the message is delivered.
    # Give the loop time to consume, then set stop (which we also fire from
    # subscriber_loop's wait path by unblocking listen()).
    def stop_after_short_delay():
        time.sleep(0.05)
        stop.set()
        block.set()                     # unblock listen() so the thread exits

    threading.Thread(target=stop_after_short_delay, daemon=True).start()
    subscriber_loop(
        factory, buf, bus,
        stop_event=stop,
        reconnect_delay_seconds=0.01,
    )

    # Ring received the envelope.
    snap = buf.snapshot()
    assert len(snap["smartload.anomaly"]) == 1
    assert snap["smartload.anomaly"][0]["payload"]["backend_id"] == "b1"

    # Subscriber received it via the bus.
    entry = q.get_nowait()
    assert entry["payload"]["status"] == "unhealthy"


def test_subscriber_loop_reconnects_on_exception():
    """First factory call raises, second succeeds. The loop must back off and
    retry, not crash."""
    raw_fc = _envelope_bytes("smartload.forecast", {"predicted_rps": 100.0})
    messages = [
        {"type": "message", "channel": b"smartload.forecast", "data": raw_fc},
    ]

    call_count = {"n": 0}
    stop = threading.Event()
    block = threading.Event()

    def factory():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated redis down")
        return _FakeRedisClient(messages, block)

    buf = RingBuffer()
    bus = EngineEventBus()

    def stop_after():
        time.sleep(0.15)
        stop.set()
        block.set()
    threading.Thread(target=stop_after, daemon=True).start()

    subscriber_loop(
        factory, buf, bus,
        stop_event=stop,
        reconnect_delay_seconds=0.01,
    )

    # The first attempt raised, the second delivered the message exactly once.
    assert call_count["n"] >= 2
    assert len(buf.snapshot()["smartload.forecast"]) == 1


def test_subscriber_loop_ignores_non_message_frames():
    """Redis can yield 'subscribe' / 'pong' frames too — they must be
    silently ignored."""
    messages = [
        {"type": "subscribe", "channel": b"smartload.anomaly"},
        {"type": "pong",      "channel": b"smartload.anomaly"},
    ]
    buf = RingBuffer()
    bus = EngineEventBus()
    stop = threading.Event()
    block = threading.Event()

    def stop_after():
        time.sleep(0.05)
        stop.set()
        block.set()
    threading.Thread(target=stop_after, daemon=True).start()

    subscriber_loop(
        lambda: _FakeRedisClient(messages, block), buf, bus,
        stop_event=stop, reconnect_delay_seconds=0.01,
    )
    # Nothing landed in the buffer.
    for ch in CHANNELS:
        assert buf.snapshot()[ch] == []
