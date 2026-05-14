"""Redis pub/sub helpers. Channel names + envelope decoding + background subscriber."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Callable

from ._envelope import (
    CHANNEL_ANOMALY,
    CHANNEL_FORECAST,
    CHANNEL_POLICY,
    CHANNEL_ROUTING,
    CHANNEL_SCALE,
    parse_envelope,
)

if TYPE_CHECKING:
    from .client import SmartLoadClient

__all__ = [
    "EventsClient",
    "PolicySubscription",
    "CHANNEL_POLICY",
    "CHANNEL_ANOMALY",
    "CHANNEL_FORECAST",
    "CHANNEL_ROUTING",
    "CHANNEL_SCALE",
]

_log = logging.getLogger("smartload_client")


class PolicySubscription:
    """Handle returned by subscribe_policy().

    Stop the background subscriber thread by calling `.close()` or using
    the subscription as a context manager.
    """

    def __init__(self, pubsub, thread: threading.Thread, stop_event: threading.Event):
        self._pubsub = pubsub
        self._thread = thread
        self._stop = stop_event

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        try:
            self._pubsub.close()
        except Exception:
            pass

    def __enter__(self) -> "PolicySubscription":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class EventsClient:
    """Subscribe to SmartLoad's Redis event stream."""

    def __init__(self, parent: "SmartLoadClient"):
        self._parent = parent

    def subscribe_policy(
        self,
        callback: Callable[[dict, dict], None],
    ) -> PolicySubscription:
        """Run `callback(payload, envelope_meta)` for every PolicyUpdate envelope
        received on smartload.policy.

        The callback runs on a daemon thread. Exceptions raised by the callback
        are logged but never propagated — a buggy user callback must not kill
        the subscription thread.

        Call `.close()` on the returned PolicySubscription to stop.
        """
        redis_client = self._parent._get_redis()
        pubsub = redis_client.pubsub()
        pubsub.subscribe(CHANNEL_POLICY)

        stop = threading.Event()

        def _run() -> None:
            while not stop.is_set():
                try:
                    msg = pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=0.5,
                    )
                except Exception:
                    _log.exception("policy pubsub get_message failed; retrying")
                    if stop.wait(timeout=1.0):
                        return
                    continue
                if msg is None or msg.get("type") != "message":
                    continue
                parsed = parse_envelope(msg.get("data", b""), channel=CHANNEL_POLICY)
                if parsed is None:
                    continue
                payload, meta = parsed
                try:
                    callback(payload, meta)
                except Exception:
                    _log.exception("policy subscriber callback raised")

        thread = threading.Thread(
            target=_run, daemon=True, name="smartload-policy-sub",
        )
        thread.start()
        return PolicySubscription(pubsub, thread, stop)

    # ── deferred (slice #1 scope) ──────────────────────────────────────────

    def subscribe_anomaly(self, callback):
        raise NotImplementedError("Deferred; see issue #127 (full SDK)")

    def subscribe_forecast(self, callback):
        raise NotImplementedError("Deferred; see issue #127 (full SDK)")

    def subscribe_routing(self, callback):
        raise NotImplementedError("Deferred; see issue #127 (full SDK)")

    def subscribe_scale(self, callback):
        raise NotImplementedError("Deferred; see issue #127 (full SDK)")
