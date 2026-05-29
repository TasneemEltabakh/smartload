"""Live Engines surface.

Exposes the three AI services' `/api/v1/engine/state` endpoint plus the
operator-UI BFF aggregator (`/api/ui/engines/snapshot`) and SSE stream
(`/api/ui/engines/stream`) — slice #121 (OUI.3, session 1 shipped
2026-05-24; SDK methods session 2 shipped this release).

Three methods on the sub-client:

    client.engines.snapshot() -> dict
        Synchronous fan-out across all three AI services + recent ring
        snapshots for the four event channels. Single HTTP GET against the
        operator-UI BFF.

    client.engines.state(service) -> dict
        Per-engine canonical `/api/v1/engine/state` body, hit directly
        against the service's port. Used when you want raw engine state
        without the BFF aggregation layer.

    client.engines.subscribe(callback) -> EnginesSubscription
        SSE consumer of `/api/ui/engines/stream`. Callback runs on a
        background daemon thread per event. Optional `channels` argument
        filters client-side; the BFF stream always carries all four
        channels. Returns a handle whose `.close()` stops the subscriber.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Callable, Iterable, Literal, Optional

import httpx

from .exceptions import (
    AuthenticationError,
    RateLimitError,
    SmartLoadError,
    ValidationError,
)

if TYPE_CHECKING:
    from .client import SmartLoadClient

__all__ = [
    "EnginesClient",
    "EnginesSubscription",
    "EngineService",
    "EngineChannel",
]

_log = logging.getLogger("smartload_client")

EngineService = Literal["anomaly-detector", "forecasting", "rl-engine"]
EngineChannel = Literal[
    "smartload.anomaly",
    "smartload.forecast",
    "smartload.routing",
    "smartload.scale",
]

_SERVICE_PORTS: dict[str, int] = {
    "anomaly-detector": 8082,
    "forecasting":      8083,
    "rl-engine":        8084,
}


def _raise_for_status(r: httpx.Response) -> None:
    """Same status-code mapping the other sub-clients use."""
    if 200 <= r.status_code < 300:
        return
    body: dict = {}
    try:
        body = r.json()
    except (ValueError, TypeError):
        body = {}
    message = (
        body.get("error")
        or body.get("message")
        or r.text[:200]
        or f"HTTP {r.status_code}"
    )
    if r.status_code == 400:
        raise ValidationError(message, field=body.get("field"))
    if r.status_code in (401, 403):
        raise AuthenticationError(message)
    if r.status_code == 429:
        retry_after_raw = r.headers.get("Retry-After")
        try:
            retry_after = int(retry_after_raw) if retry_after_raw else None
        except (TypeError, ValueError):
            retry_after = None
        raise RateLimitError(message, retry_after=retry_after)
    raise SmartLoadError(f"HTTP {r.status_code}: {message}")


class EnginesSubscription:
    """Handle returned by EnginesClient.subscribe().

    Call `.close()` to stop the background SSE consumer, or use as a
    context manager.
    """

    def __init__(
        self,
        response,
        thread: threading.Thread,
        stop_event: threading.Event,
    ) -> None:
        self._response = response
        self._thread = thread
        self._stop = stop_event

    def close(self) -> None:
        self._stop.set()
        try:
            self._response.close()
        except Exception:                            # noqa: BLE001
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "EnginesSubscription":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class EnginesClient:
    """Live engine state via BFF aggregator + per-engine direct endpoints."""

    def __init__(self, parent: "SmartLoadClient") -> None:
        self._parent = parent

    # ── snapshot (one shot) ───────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Synchronous snapshot of all three engines + per-channel rings.

        Returns the operator-UI BFF's aggregated response:
            {
                "services": {<service>: <engine_state>, ...},
                "channels": {<channel>: [<envelope>, ...], ...},
                "recent":   [<envelope>, ...],   # merged + sorted
            }
        """
        url = f"{self._parent.operator_ui_url}/api/ui/engines/snapshot"
        with httpx.Client(timeout=self._parent.timeout) as client:
            r = client.get(url)
            _raise_for_status(r)
            return r.json()

    # ── per-engine state ──────────────────────────────────────────────────────

    def state(self, service: EngineService) -> dict:
        """Per-engine canonical `/api/v1/engine/state` body.

        Bypasses the operator-UI BFF and hits the service directly. Useful
        when you want raw engine state without the parallel-fan-out cost or
        when an SDK consumer doesn't have the operator-UI deployed.
        """
        if service not in _SERVICE_PORTS:
            raise ValidationError(
                f"unknown engine service {service!r}; expected one of "
                f"{sorted(_SERVICE_PORTS)}",
                field="service",
            )
        base = self._parent._engine_url(service)
        url = f"{base}/api/v1/engine/state"
        with httpx.Client(timeout=self._parent.timeout) as client:
            r = client.get(url)
            _raise_for_status(r)
            return r.json()

    # ── SSE subscribe ─────────────────────────────────────────────────────────

    def subscribe(
        self,
        callback: Callable[[str, dict, dict], None],
        *,
        channels: Optional[Iterable[EngineChannel]] = None,
    ) -> EnginesSubscription:
        """Run `callback(channel, payload, envelope_meta)` for every SSE event.

        The callback runs on a daemon thread. Exceptions are logged but
        never propagated — a buggy user callback must not kill the
        subscription thread.

        Args:
            callback: invoked as `callback(channel_str, payload_dict,
                envelope_meta_dict)` for each parsed event.
            channels: optional client-side filter. The BFF stream carries
                all four engine channels; pass a subset to receive only
                those. Unknown channel names are ignored.

        Returns:
            EnginesSubscription handle. Call `.close()` to stop.
        """
        filter_set: Optional[set[str]] = set(channels) if channels else None
        url = f"{self._parent.operator_ui_url}/api/ui/engines/stream"
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        try:
            response = urllib.request.urlopen(req, timeout=self._parent.timeout)
        except urllib.error.HTTPError as exc:
            raise SmartLoadError(f"SSE stream returned {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise SmartLoadError(f"could not open SSE stream: {exc.reason}") from exc

        stop_event = threading.Event()

        def _drain() -> None:
            try:
                pending_channel: Optional[str] = None
                while not stop_event.is_set():
                    line = response.readline()
                    if not line:
                        # Empty read = server closed. Exit the loop and let
                        # the subscription handle expose .close() to the
                        # caller for cleanup.
                        break
                    text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not text:
                        pending_channel = None        # event delimiter
                        continue
                    if text.startswith(":"):
                        continue                      # heartbeat comment
                    if text.startswith("event:"):
                        pending_channel = text[len("event:"):].strip() or None
                        continue
                    if text.startswith("data:"):
                        raw = text[len("data:"):].strip()
                        try:
                            frame = json.loads(raw)
                        except (ValueError, TypeError):
                            continue
                        # Two shapes the BFF can send:
                        #   1. {"channel": ..., "envelope": {...}, "payload": {...}}
                        #   2. {"channel": ..., "envelope": {..., "payload": {...}}}
                        channel = frame.get("channel") or pending_channel or ""
                        if filter_set is not None and channel not in filter_set:
                            continue
                        envelope = frame.get("envelope") or {}
                        payload = (
                            frame.get("payload")
                            or envelope.get("payload")
                            or {}
                        )
                        meta = {k: v for k, v in envelope.items() if k != "payload"}
                        try:
                            callback(channel, payload, meta)
                        except Exception as exc:      # noqa: BLE001
                            _log.warning(
                                "engines.subscribe callback raised: %s", exc,
                            )
            except Exception as exc:                  # noqa: BLE001
                # `.close()` while the thread is blocked in readline() raises
                # from inside http.client — that's the expected shutdown
                # path, not a real error. Only log if it wasn't a controlled
                # stop.
                if not stop_event.is_set():
                    _log.warning(
                        "engines.subscribe drain loop exited: %s", exc,
                    )
            finally:
                try:
                    response.close()
                except Exception:                    # noqa: BLE001
                    pass

        thread = threading.Thread(
            target=_drain,
            daemon=True,
            name="smartload-engines-sse",
        )
        thread.start()

        return EnginesSubscription(response, thread, stop_event)
