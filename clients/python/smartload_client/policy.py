"""Policy endpoints. Pairs with services/policy-manager."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from .exceptions import (
    AuthenticationError,
    RateLimitError,
    SmartLoadError,
    ValidationError,
)

if TYPE_CHECKING:
    from .client import SmartLoadClient


def _raise_for_status(r: httpx.Response) -> None:
    """Map HTTP status codes to typed exceptions.

      400 → ValidationError(message, field=body.field)
      401, 403 → AuthenticationError
      429 → RateLimitError(retry_after=Retry-After)
      5xx and any other non-2xx → SmartLoadError
    """
    if 200 <= r.status_code < 300:
        return
    body: dict[str, Any]
    try:
        body = r.json()
    except (ValueError, TypeError):
        body = {}
    message = body.get("error") or body.get("message") or r.text[:200] or f"HTTP {r.status_code}"
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


class PolicyClient:
    """HTTP client for /api/v1/policy and /api/v1/audit/policy."""

    def __init__(self, parent: "SmartLoadClient"):
        self._parent = parent

    def get(self) -> dict:
        """Return the current operating policy as a dict."""
        try:
            r = self._parent._http.get("/api/v1/policy")
        except httpx.RequestError as exc:
            raise SmartLoadError(f"policy GET failed: {exc}") from exc
        _raise_for_status(r)
        return r.json()

    def update(self, patch: dict, *, actor: str | None = None) -> dict:
        """Propose + commit a policy change.

        Returns the policy-manager's full response body:
          {status, policy, changed_fields, policy_version, event_id}

        `status` is "updated" on a real change or "no-op" on idempotent retry.
        """
        headers: dict[str, str] = {}
        headers["X-Actor"] = actor or self._parent.default_actor
        try:
            r = self._parent._http.post(
                "/api/v1/policy", json=patch, headers=headers,
            )
        except httpx.RequestError as exc:
            raise SmartLoadError(f"policy POST failed: {exc}") from exc
        _raise_for_status(r)
        return r.json()

    def set_strategy(self, name: str, *, actor: str | None = None) -> dict:
        """Apply a named load-balancing strategy via POST /api/v1/policy/strategy.

        Translates an industry-vocabulary strategy name (round-robin,
        least-connections, latency-aware, forecast-aware, anomaly-aware,
        ai-hybrid, safe-fallback) to the underlying policy primitives
        (operating_mode + safe_mode) server-side, applying them through the same
        audit + envelope path as `update()`.

        Returns the policy-manager's response body:
          {status, policy, changed_fields, policy_version, event_id,
           strategy, recommended_rl_mode}

        `recommended_rl_mode` is the deploy-time RL_MODE env pin that matches the
        chosen strategy ("shadow" / "active" / null) — surfaced for the operator,
        never set as a policy field.

        An unknown strategy name raises ValidationError with field='name'.
        """
        body: dict[str, Any] = {"name": name}
        headers: dict[str, str] = {"X-Actor": actor or self._parent.default_actor}
        try:
            r = self._parent._http.post(
                "/api/v1/policy/strategy", json=body, headers=headers,
            )
        except httpx.RequestError as exc:
            raise SmartLoadError(f"strategy POST failed: {exc}") from exc
        _raise_for_status(r)
        return r.json()

    def audit(self, limit: int = 50) -> list[dict]:
        """Return recent policy_changes rows, newest first."""
        try:
            r = self._parent._http.get(
                "/api/v1/audit/policy", params={"limit": limit},
            )
        except httpx.RequestError as exc:
            raise SmartLoadError(f"policy audit GET failed: {exc}") from exc
        _raise_for_status(r)
        return r.json()
