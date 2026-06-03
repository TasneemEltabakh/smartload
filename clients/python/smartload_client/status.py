"""
clients/python/smartload_client/status.py
──────────────────────────────────────────
Consolidated status surface (slice #149 / OUI.9).

`GET /api/v1/status` on the operator-UI BFF returns one document that
collapses what would otherwise be ~9 separate calls (8 service /health +
GET /api/v1/policy + audit reads). The SDK presents it as a typed
`StatusResponse` dataclass so downstream code doesn't have to remember
the response shape.

Access via the top-level convenience on `SmartLoadClient`:

    with SmartLoadClient() as c:
        status = c.get_status()
        if status.overall != "ok":
            for name, svc in status.services.items():
                if svc.status != "ok":
                    print(f"{name}: {svc.status} ({svc.extra})")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

import httpx

from .exceptions import SmartLoadError

if TYPE_CHECKING:
    from .client import SmartLoadClient


__all__ = [
    "ServiceStatus",
    "ActivePolicySnapshot",
    "RecentEvents",
    "StatusResponse",
    "StatusClient",
]


@dataclass
class ServiceStatus:
    """One service's slice of the `/api/v1/status` response.

    `status` is the canonical pill ("ok" | "degraded" | "down" | "unknown");
    `extra` is the rest of the service's `/health` body — service-specific
    fields like `policy_version`, `engine`, `active_target_count`, etc.
    """

    name: str
    status: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActivePolicySnapshot:
    """Trimmed view of `GET /api/v1/policy` — the fields most operators
    care about for at-a-glance triage. The full policy is available via
    `client.get_policy()`."""

    operating_mode: Optional[str] = None
    safe_mode: Optional[bool] = None
    slo_p95_latency_ms: Optional[int] = None
    policy_version: Optional[int] = None


@dataclass
class RecentEvents:
    """Most recent rows from the two audit streams. Either field may be
    None — best-effort fetches; service degradation never blocks the
    overall status read."""

    last_policy_change: Optional[dict] = None
    last_scaling_event: Optional[dict] = None


@dataclass
class StatusResponse:
    """Full `/api/v1/status` response. `from_dict()` parses the raw JSON
    coming off the wire; round-trip-safe with `to_dict()` for tests."""

    generated_at: str
    overall: str
    services: dict[str, ServiceStatus]
    active_policy: Optional[ActivePolicySnapshot] = None
    recent: RecentEvents = field(default_factory=RecentEvents)

    @classmethod
    def from_dict(cls, data: dict) -> "StatusResponse":
        services_raw = data.get("services") or {}
        services = {
            name: ServiceStatus(
                name=name,
                status=str(svc.get("status", "unknown")),
                extra={k: v for k, v in svc.items() if k != "status"},
            )
            for name, svc in services_raw.items()
        }
        ap_raw = data.get("active_policy")
        active_policy = ActivePolicySnapshot(
            operating_mode=ap_raw.get("operating_mode") if isinstance(ap_raw, dict) else None,
            safe_mode=ap_raw.get("safe_mode") if isinstance(ap_raw, dict) else None,
            slo_p95_latency_ms=ap_raw.get("slo_p95_latency_ms") if isinstance(ap_raw, dict) else None,
            policy_version=ap_raw.get("policy_version") if isinstance(ap_raw, dict) else None,
        ) if ap_raw else None
        recent_raw = data.get("recent") or {}
        recent = RecentEvents(
            last_policy_change=recent_raw.get("last_policy_change"),
            last_scaling_event=recent_raw.get("last_scaling_event"),
        )
        return cls(
            generated_at=str(data.get("generated_at", "")),
            overall=str(data.get("overall", "unknown")),
            services=services,
            active_policy=active_policy,
            recent=recent,
        )

    def to_dict(self) -> dict:
        out: dict = {
            "generated_at": self.generated_at,
            "overall": self.overall,
            "services": {
                name: {"status": svc.status, **svc.extra}
                for name, svc in self.services.items()
            },
            "recent": {
                "last_policy_change": self.recent.last_policy_change,
                "last_scaling_event": self.recent.last_scaling_event,
            },
        }
        if self.active_policy is not None:
            out["active_policy"] = {
                "operating_mode": self.active_policy.operating_mode,
                "safe_mode": self.active_policy.safe_mode,
                "slo_p95_latency_ms": self.active_policy.slo_p95_latency_ms,
                "policy_version": self.active_policy.policy_version,
            }
        else:
            out["active_policy"] = None
        return out


class StatusClient:
    """Single-call wrapper for `GET /api/v1/status`. Constructed by the
    parent `SmartLoadClient`; not usually instantiated directly."""

    def __init__(self, parent: "SmartLoadClient") -> None:
        self._parent = parent

    def get(self) -> StatusResponse:
        """Fetch the consolidated status response. Always returns the
        parsed `StatusResponse` — caller can check `.overall` for the
        rolled-up pill or iterate `services` for per-service detail."""
        url = f"{self._parent.operator_ui_url}/api/v1/status"
        try:
            with httpx.Client(timeout=self._parent.timeout) as client:
                r = client.get(url)
        except httpx.RequestError as exc:
            raise SmartLoadError(f"status: request failed ({type(exc).__name__})") from exc
        if r.status_code != 200:
            raise SmartLoadError(f"status: unexpected HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError as exc:
            raise SmartLoadError("status: response was not valid JSON") from exc
        if not isinstance(body, dict):
            raise SmartLoadError("status: response was not a JSON object")
        return StatusResponse.from_dict(body)
