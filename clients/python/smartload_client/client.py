"""Top-level SmartLoad client. Aggregates sub-clients per surface."""

from __future__ import annotations

import os
from typing import Optional

import httpx

from .audit import AuditClient, AuditKind
from .events import EventsClient
from .metrics import MetricsClient
from .policy import PolicyClient

__all__ = ["SmartLoadClient"]


class SmartLoadClient:
    """Single entrypoint for using SmartLoad from Python.

    Usage:
        with SmartLoadClient(base_url="http://localhost:8086") as c:
            policy = c.get_policy()
            c.set_policy({"safe_mode": True}, actor="my-tool")

    Attributes used as auth headers when present:
      - api_key   → `Authorization: Bearer <key>` (and `X-API-Key`, redundant
                    for the duration the live policy-manager still ignores
                    Authorization)
      - tenant_id → `X-Tenant-Id`
      - default actor → `X-Actor` (overridable per-call via methods that
                    accept `actor=`)

    `redis_url` is used only when `subscribe_*` methods are called; the
    Redis client is created lazily so the SDK works without Redis when only
    HTTP endpoints are used.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8086",
        autoscaler_url: Optional[str] = None,
        redis_url: Optional[str] = None,
        api_key: Optional[str] = None,
        tenant_id: Optional[str] = None,
        default_actor: str = "smartload-client",
        timeout: float = 10.0,
        connect_timeout: float = 3.0,
    ):
        self.base_url = base_url.rstrip("/")
        # The scaling audit stream lives on the autoscaler (port 8085), which
        # is a different upstream than the policy-manager that base_url points
        # at. Defaults to localhost:8085 for dev; override in production.
        self.autoscaler_url = (
            autoscaler_url
            or os.environ.get("SMARTLOAD_AUTOSCALER_URL", "http://localhost:8085")
        ).rstrip("/")
        self.timeout = timeout
        self.redis_url = redis_url or os.environ.get(
            "REDIS_URL", "redis://localhost:6379"
        )
        self.api_key = api_key or os.environ.get("SMARTLOAD_API_KEY")
        self.tenant_id = tenant_id or os.environ.get(
            "SMARTLOAD_TENANT_ID", "default"
        )
        self.default_actor = default_actor

        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key
        if self.tenant_id:
            headers["X-Tenant-Id"] = self.tenant_id

        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            headers=headers,
        )
        self._redis = None  # lazy

        self.policy = PolicyClient(self)
        self.metrics = MetricsClient(self)
        self.events = EventsClient(self)
        self.audit = AuditClient(self)

    # ── lifecycle ──────────────────────────────────────────────────────────

    def __enter__(self) -> "SmartLoadClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:
            pass
        if self._redis is not None:
            try:
                self._redis.close()
            except Exception:
                pass
            self._redis = None

    # ── lazy redis ─────────────────────────────────────────────────────────

    def _get_redis(self):
        """Lazily create and cache the Redis client.

        The SDK works without Redis for HTTP-only flows; Redis is only
        constructed the first time a `subscribe_*` method is called.
        """
        if self._redis is None:
            import redis as redis_lib  # local import keeps import-time cost low
            self._redis = redis_lib.from_url(
                self.redis_url, decode_responses=False
            )
        return self._redis

    # ── delegated convenience methods (policy surface) ─────────────────────

    def get_policy(self) -> dict:
        return self.policy.get()

    def set_policy(self, patch: dict, *, actor: str | None = None) -> dict:
        return self.policy.update(patch, actor=actor)

    def audit_policy(self, limit: int = 50) -> list[dict]:
        return self.policy.audit(limit=limit)

    def list_audit(self, kind: AuditKind, limit: int = 50) -> list[dict]:
        """Audit-log convenience: dispatch by kind across the two streams.

        kind="policy"  → policy_changes (policy-manager)
        kind="scaling" → scaling_events (autoscaler)
        """
        return self.audit.list(kind, limit=limit)

    def subscribe_policy(self, callback):
        return self.events.subscribe_policy(callback)

    # ── deferred surfaces (slice #1 scope) ─────────────────────────────────

    def get_metrics(self, service: str, window: str = "5m"):
        return self.metrics.read(service, window)

    def subscribe_anomaly(self, callback):
        raise NotImplementedError("Deferred; see issue #127 (full SDK)")

    def subscribe_forecast(self, callback):
        raise NotImplementedError("Deferred; see issue #127 (full SDK)")

    def subscribe_routing(self, callback):
        raise NotImplementedError("Deferred; see issue #127 (full SDK)")

    def subscribe_scale(self, callback):
        raise NotImplementedError("Deferred; see issue #127 (full SDK)")
