"""Audit endpoints. Slice #2 (#122) — read-only audit-log viewer.

Audit rows live in two hypertables owned by two different services:
  - policy_changes  → written by policy-manager;  GET /api/v1/audit/policy
  - scaling_events  → written by autoscaler;      GET /api/v1/audit/scaling

This sub-client normalises both into a single SDK surface so integrators
don't have to know which service owns which stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import httpx

from .exceptions import (
    AuthenticationError,
    RateLimitError,
    SmartLoadError,
    ValidationError,
)

if TYPE_CHECKING:
    from .client import SmartLoadClient


AuditKind = Literal["policy", "scaling"]


def _raise_for_status(r: httpx.Response) -> None:
    """Same status-code mapping as the policy module — kept local so the
    audit sub-client doesn't depend on policy.py's private helper."""
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


class AuditClient:
    """HTTP client for /api/v1/audit/policy and /api/v1/audit/scaling.

    Each audit stream lives on a different upstream service, so this client
    keeps its own per-kind base URLs:

      - policy   → parent.base_url       (policy-manager, default :8086)
      - scaling  → parent.autoscaler_url (autoscaler,     default :8085)
    """

    def __init__(self, parent: "SmartLoadClient"):
        self._parent = parent

    def policy(self, limit: int = 50) -> list[dict]:
        """Return recent policy_changes rows, newest first."""
        try:
            r = self._parent._http.get(
                "/api/v1/audit/policy", params={"limit": limit},
            )
        except httpx.RequestError as exc:
            raise SmartLoadError(f"policy audit GET failed: {exc}") from exc
        _raise_for_status(r)
        return r.json()

    def scaling(self, limit: int = 50) -> list[dict]:
        """Return recent scaling_events rows, newest first."""
        url = f"{self._parent.autoscaler_url}/api/v1/audit/scaling"
        try:
            r = httpx.get(url, params={"limit": limit},
                          timeout=self._parent.timeout)
        except httpx.RequestError as exc:
            raise SmartLoadError(f"scaling audit GET failed: {exc}") from exc
        _raise_for_status(r)
        return r.json()

    def list(self, kind: AuditKind, limit: int = 50) -> list[dict]:
        """Dispatch to the right audit stream by kind.

        kind="policy"  → policy_changes
        kind="scaling" → scaling_events
        """
        if kind == "policy":
            return self.policy(limit=limit)
        if kind == "scaling":
            return self.scaling(limit=limit)
        raise ValidationError(
            f"unknown audit kind: {kind!r} (expected 'policy' or 'scaling')",
            field="kind",
        )
