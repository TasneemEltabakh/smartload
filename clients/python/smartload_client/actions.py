"""Manual actions endpoints. Slice #3 (#123) — operator override surface.

Two endpoints across two services:
  - POST /api/v1/scale    on autoscaler        (port 8085)
  - POST /api/v1/isolate  on anomaly-detector  (port 8082)

Both bypass automatic decisions; the operator's intent is the signal.
Both write audit rows that show up in the audit-log slice's UI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Optional

import httpx

from .exceptions import (
    AuthenticationError,
    RateLimitError,
    SmartLoadError,
    ValidationError,
)

if TYPE_CHECKING:
    from .client import SmartLoadClient


IsolateStatus = Literal["healthy", "degraded", "unhealthy"]


def _raise_for_status(r: httpx.Response) -> None:
    """Local copy of the status-code mapper (kept independent of policy.py)."""
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


class ActionsClient:
    """HTTP client for the manual-action endpoints.

    Each action lives on its own upstream service:
      - scale()   → autoscaler         (parent.autoscaler_url)
      - isolate() → anomaly-detector   (parent.anomaly_detector_url)
    """

    def __init__(self, parent: "SmartLoadClient"):
        self._parent = parent

    def scale(
        self,
        target_count: int,
        *,
        actor: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> dict:
        """Manually scale the backend pool to `target_count` instances.

        Bypasses forecast subscription + cooldown. The target must satisfy
        policy.min_backends <= target_count <= policy.max_backends; otherwise
        a ValidationError is raised with field='target_count'.

        Returns the autoscaler's response body:
          {
            "status":          "applied" | "noop",
            "action":          "scale_out" | "scale_in" | "noop",
            "target_count":    int,
            "previous_count":  int,
            "final_count":     int,
            "steps_actuated":  int,
            "steps_requested": int,
            "reason":          "manual:<actor>: <reason>",
            "event_id":        <uuid>,
          }
        """
        body: dict = {"target_count": target_count}
        if actor is not None:
            body["actor"] = actor
        elif self._parent.default_actor:
            body["actor"] = self._parent.default_actor
        if reason is not None:
            body["reason"] = reason

        url = f"{self._parent.autoscaler_url}/api/v1/scale"
        try:
            r = httpx.post(url, json=body, timeout=self._parent.timeout)
        except httpx.RequestError as exc:
            raise SmartLoadError(f"scale POST failed: {exc}") from exc
        _raise_for_status(r)
        return r.json()

    def isolate(
        self,
        backend_id: str,
        status: IsolateStatus = "unhealthy",
        *,
        actor: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> dict:
        """Publish a synthetic AnomalyEvent for `backend_id`.

        Useful for demoing the anomaly-reroute path (LB sidecar / T2.1
        consumers) without inducing real backend failure. The
        anomaly-detector writes a backend_health row and publishes the
        envelope on smartload.anomaly.

        status must be one of healthy / degraded / unhealthy; an unknown
        value raises ValidationError with field='status'.

        Returns:
          {
            "status":         "applied",
            "backend_id":     str,
            "anomaly_status": "healthy" | "degraded" | "unhealthy",
            "score":          0.0 | 1.0,
            "actor":          str,
            "reason":         "manual:<actor>: <reason>",
            "event_id":       <uuid>,
          }
        """
        body: dict = {"backend_id": backend_id, "status": status}
        if actor is not None:
            body["actor"] = actor
        elif self._parent.default_actor:
            body["actor"] = self._parent.default_actor
        if reason is not None:
            body["reason"] = reason

        url = f"{self._parent.anomaly_detector_url}/api/v1/isolate"
        try:
            r = httpx.post(url, json=body, timeout=self._parent.timeout)
        except httpx.RequestError as exc:
            raise SmartLoadError(f"isolate POST failed: {exc}") from exc
        _raise_for_status(r)
        return r.json()
