"""Top-level SmartLoad client. Aggregates sub-clients per surface."""

from __future__ import annotations

import os
from typing import Optional

import httpx

from .actions import ActionsClient, IsolateStatus
from .audit import AuditClient, AuditKind
from .engines import EnginesClient, EngineService
from .events import EventsClient
from .metrics import MetricsClient
from .policy import PolicyClient
from .status import StatusClient, StatusResponse

__all__ = ["SmartLoadClient"]


# Default per-service URLs for the EnginesClient.state(service) direct path.
# Override via the SDK constructor kwargs or the SMARTLOAD_*_URL env vars.
_DEFAULT_ENGINE_URLS: dict[str, tuple[str, str]] = {
    "anomaly-detector": ("SMARTLOAD_ANOMALY_DETECTOR_URL", "http://localhost:8082"),
    "forecasting":      ("SMARTLOAD_FORECASTING_URL",      "http://localhost:8083"),
    "rl-engine":        ("SMARTLOAD_RL_ENGINE_URL",        "http://localhost:8084"),
}


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
        anomaly_detector_url: Optional[str] = None,
        forecasting_url: Optional[str] = None,
        rl_engine_url: Optional[str] = None,
        operator_ui_url: Optional[str] = None,
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
        # The manual-isolate endpoint lives on anomaly-detector (port 8082);
        # same per-service-upstream pattern as autoscaler_url. Override in
        # production deployments where services are behind a gateway.
        self.anomaly_detector_url = (
            anomaly_detector_url
            or os.environ.get(
                "SMARTLOAD_ANOMALY_DETECTOR_URL", "http://localhost:8082",
            )
        ).rstrip("/")
        # Forecasting + rl-engine for client.engines.state(service) direct
        # calls. Same per-service-upstream pattern as the others.
        self.forecasting_url = (
            forecasting_url
            or os.environ.get("SMARTLOAD_FORECASTING_URL", "http://localhost:8083")
        ).rstrip("/")
        self.rl_engine_url = (
            rl_engine_url
            or os.environ.get("SMARTLOAD_RL_ENGINE_URL", "http://localhost:8084")
        ).rstrip("/")
        # Operator-UI BFF for engines.snapshot() + engines.subscribe(). The
        # BFF is the aggregator across the three AI services + holds the
        # per-channel ring buffers + serves the SSE stream.
        self.operator_ui_url = (
            operator_ui_url
            or os.environ.get("SMARTLOAD_OPERATOR_UI_URL", "http://localhost:8090")
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
        self.actions = ActionsClient(self)
        self.engines = EnginesClient(self)
        self.status = StatusClient(self)

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

    def set_strategy(self, name: str, *, actor: str | None = None) -> dict:
        """Apply a named load-balancing strategy (#150). Convenience wrapper
        around client.policy.set_strategy()."""
        return self.policy.set_strategy(name, actor=actor)

    def audit_policy(self, limit: int = 50) -> list[dict]:
        return self.policy.audit(limit=limit)

    def list_audit(self, kind: AuditKind, limit: int = 50) -> list[dict]:
        """Audit-log convenience: dispatch by kind across the two streams.

        kind="policy"  → policy_changes (policy-manager)
        kind="scaling" → scaling_events (autoscaler)
        """
        return self.audit.list(kind, limit=limit)

    # ── manual actions (slice #3, #123) ────────────────────────────────────

    def scale(
        self,
        target_count: int,
        *,
        actor: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> dict:
        """Manually scale the backend pool. Convenience wrapper around
        client.actions.scale()."""
        return self.actions.scale(target_count, actor=actor, reason=reason)

    def isolate(
        self,
        backend_id: str,
        status: IsolateStatus = "unhealthy",
        *,
        actor: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> dict:
        """Manually publish an AnomalyEvent. Convenience wrapper around
        client.actions.isolate()."""
        return self.actions.isolate(backend_id, status, actor=actor, reason=reason)

    # ── dry-run / simulate (#146) ──────────────────────────────────────────

    def simulate_scale(
        self,
        target_count: int,
        *,
        actor: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> dict:
        """Preview a manual scale without actuating. Convenience wrapper around
        client.actions.simulate_scale()."""
        return self.actions.simulate_scale(target_count, actor=actor, reason=reason)

    def simulate_isolate(
        self,
        backend_id: str,
        status: IsolateStatus = "unhealthy",
        *,
        actor: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> dict:
        """Preview a manual isolate without publishing. Convenience wrapper
        around client.actions.simulate_isolate()."""
        return self.actions.simulate_isolate(backend_id, status, actor=actor, reason=reason)

    def subscribe_policy(self, callback):
        return self.events.subscribe_policy(callback)

    # ── consolidated status (slice #149 / OUI.9) ────────────────────────────

    def get_status(self) -> StatusResponse:
        """One-shot aggregate read across every service + active policy +
        most recent audit rows. Replaces the 7-call polling burst
        (six /health + GET /api/v1/policy) integrators used to need.

        Hits the operator-UI BFF at `/api/v1/status`. Always returns the
        parsed `StatusResponse` — callers check `.overall` for the
        rolled-up pill ("ok" | "degraded" | "down") or iterate
        `.services` for per-service detail."""
        return self.status.get()

    # ── live engines (slice #121, session 2) ───────────────────────────────

    def engines_snapshot(self) -> dict:
        """Aggregated snapshot of all three AI engines + per-channel rings.
        Convenience wrapper around client.engines.snapshot()."""
        return self.engines.snapshot()

    def engines_state(self, service: EngineService) -> dict:
        """Per-engine canonical /api/v1/engine/state body. Convenience
        wrapper around client.engines.state(service)."""
        return self.engines.state(service)

    def subscribe_engines(self, callback, *, channels=None):
        """SSE consumer of /api/ui/engines/stream. Convenience wrapper
        around client.engines.subscribe()."""
        return self.engines.subscribe(callback, channels=channels)

    # ── per-service URL resolver (used by EnginesClient.state) ─────────────

    def _engine_url(self, service: str) -> str:
        """Resolve the canonical URL for a per-engine state call."""
        if service == "anomaly-detector":
            return self.anomaly_detector_url
        if service == "forecasting":
            return self.forecasting_url
        if service == "rl-engine":
            return self.rl_engine_url
        raise ValueError(f"unknown engine service: {service!r}")

    # ── deferred surfaces (slice #1 scope) ─────────────────────────────────

    def get_metrics(self, service: str, window: str = "5m"):
        return self.metrics.read(service, window)

    def subscribe_anomaly(self, callback):
        """Subscribe to only smartload.anomaly via the SSE stream — implemented
        as a single-channel filter over engines.subscribe()."""
        return self.engines.subscribe(callback, channels=["smartload.anomaly"])

    def subscribe_forecast(self, callback):
        """Subscribe to only smartload.forecast via the SSE stream."""
        return self.engines.subscribe(callback, channels=["smartload.forecast"])

    def subscribe_routing(self, callback):
        """Subscribe to only smartload.routing via the SSE stream."""
        return self.engines.subscribe(callback, channels=["smartload.routing"])

    def subscribe_scale(self, callback):
        """Subscribe to only smartload.scale via the SSE stream."""
        return self.engines.subscribe(callback, channels=["smartload.scale"])
