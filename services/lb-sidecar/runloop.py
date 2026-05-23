"""
services/lb-sidecar/runloop.py
───────────────────────────────
Pure-Python pieces of the lb-sidecar run loop, separated from app.py so
they can be unit-tested without Flask, Redis, or a live Docker daemon.

app.py owns:
  - sockets and threads
  - live Docker client + Redis clients
  - Flask routes

This module owns:
  - BackendRegistry: Docker SDK IP→container_name mapping
  - scores_to_weights: RoutingRecommendation scores → NGINX integer weights
  - handle_routing / handle_anomaly / handle_policy: message dispatch
  - ParseResult dataclass wrapping outcomes for app.py state tracking
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional


# ── Weight calculation ────────────────────────────────────────────────────────

def scores_to_weights(server_rankings: list[dict]) -> dict[str, int]:
    """Convert RoutingRecommendation server_rankings to NGINX integer weights.

    score (float in (0, 1]) → max(1, round(score * 100)).
    backend_id keys are passed through unchanged; BackendRegistry translation
    happens in the caller before writing to the adapter.
    """
    result = {}
    for entry in server_rankings:
        backend_id = entry.get("backend_id", "")
        score = float(entry.get("score", 0.0))
        result[backend_id] = max(1, round(score * 100))
    return result


# ── BackendRegistry ───────────────────────────────────────────────────────────

class BackendRegistry:
    """Maps Docker container IPs to resolvable container names.

    rl-engine's server_rankings carry IP:port backend_ids (sourced from
    NGINX's $upstream_addr via lb-otel-shipper). NGINX's upstream block uses
    container hostnames. This class bridges the gap by querying the Docker
    SDK for running containers' network IPs and building a
    ``{ip:port → container_name:port}`` lookup table.

    Thread-safety: the internal dict is replaced atomically; callers that
    read an entry while a refresh is in progress see the previous snapshot
    (benign stale read).
    """

    def __init__(self, docker_client, seed_backends: Optional[list[str]] = None) -> None:
        self._docker = docker_client
        self._lock = threading.Lock()
        # {ip:port → container_name:port}
        self._map: dict[str, str] = {}
        self._seed = seed_backends or []
        self.refresh()

    def refresh(self) -> None:
        """Re-scan running containers and rebuild the IP→name map."""
        if self._docker is None:
            return
        try:
            containers = self._docker.containers.list()
        except Exception:  # noqa: BLE001
            return

        new_map: dict[str, str] = {}
        for c in containers:
            name = c.name  # e.g. "smartload-test-backend-1"
            networks = c.attrs.get("NetworkSettings", {}).get("Networks", {})
            for net_info in networks.values():
                ip = net_info.get("IPAddress", "")
                if not ip:
                    continue
                # Map each port the container exposes to the container name:port
                ports = c.attrs.get("NetworkSettings", {}).get("Ports", {})
                if ports:
                    for port_proto in ports:
                        port = port_proto.split("/")[0]
                        new_map[f"{ip}:{port}"] = f"{name}:{port}"
                else:
                    # Fallback: use :8080 (our default backend port)
                    new_map[f"{ip}:8080"] = f"{name}:8080"

        with self._lock:
            self._map = new_map

    def translate(self, weights: dict[str, int]) -> dict[str, int]:
        """Return a new dict with IP-based keys replaced by container names.

        Unknown entries are kept as-is. If an IP is not in the registry,
        a refresh is triggered and the translation is retried once.
        """
        with self._lock:
            snapshot = dict(self._map)

        result: dict[str, int] = {}
        needs_refresh = False
        for backend_id, weight in weights.items():
            if backend_id in snapshot:
                result[snapshot[backend_id]] = weight
            else:
                # Could be a seed backend (already a hostname) or unmapped IP
                result[backend_id] = weight
                if "." in backend_id.split(":")[0]:  # looks like an IP
                    needs_refresh = True

        if needs_refresh:
            self.refresh()
            with self._lock:
                snapshot = dict(self._map)
            result = {}
            for backend_id, weight in weights.items():
                mapped = snapshot.get(backend_id, backend_id)
                result[mapped] = weight

        return result

    def translate_one(self, backend_id: str) -> str:
        """Translate a single backend_id, refreshing once if unmapped."""
        with self._lock:
            if backend_id in self._map:
                return self._map[backend_id]

        self.refresh()
        with self._lock:
            return self._map.get(backend_id, backend_id)


# ── Message handlers ──────────────────────────────────────────────────────────

@dataclass
class RoutingOutcome:
    """Result of processing one smartload.routing message."""
    applied: bool = False
    mode: str = "shadow"
    weight_count: int = 0
    error: Optional[str] = None


@dataclass
class AnomalyOutcome:
    """Result of processing one smartload.anomaly message."""
    applied: bool = False
    backend_id: str = ""
    action: str = ""   # "exclude" | "include" | "noop"
    error: Optional[str] = None


@dataclass
class PolicyOutcome:
    """Result of processing one smartload.policy message."""
    applied: bool = False
    safe_mode: bool = False
    error: Optional[str] = None


def handle_routing(
    payload: dict,
    registry: BackendRegistry,
    adapter,
    all_backends: list[str],
) -> RoutingOutcome:
    """Process a RoutingRecommendation payload.

    Shadow-mode messages are logged only; active-mode messages trigger
    an upstream weight rewrite via the adapter.
    """
    mode = payload.get("mode", "shadow")
    rankings = payload.get("server_rankings", [])

    if mode != "active":
        return RoutingOutcome(applied=False, mode=mode, weight_count=len(rankings))

    try:
        raw_weights = scores_to_weights(rankings)
        translated = registry.translate(raw_weights)
        if not translated and all_backends:
            translated = {b: 1 for b in all_backends}
        adapter.set_upstream_weights(translated)
        return RoutingOutcome(applied=True, mode=mode, weight_count=len(translated))
    except Exception as exc:  # noqa: BLE001
        return RoutingOutcome(applied=False, mode=mode, error=str(exc))


def handle_anomaly(
    payload: dict,
    registry: BackendRegistry,
    adapter,
) -> AnomalyOutcome:
    """Process an AnomalyEvent payload.

    Unhealthy → exclude_backend; healthy/degraded → include_backend.
    """
    raw_backend_id = payload.get("backend_id", "")
    status = payload.get("status", "healthy")

    try:
        backend_name = registry.translate_one(raw_backend_id)

        if status == "unhealthy":
            adapter.exclude_backend(backend_name)
            return AnomalyOutcome(applied=True, backend_id=backend_name, action="exclude")
        else:
            adapter.include_backend(backend_name)
            return AnomalyOutcome(applied=True, backend_id=backend_name, action="include")
    except Exception as exc:  # noqa: BLE001
        return AnomalyOutcome(applied=False, backend_id=raw_backend_id, error=str(exc))


def handle_policy(
    payload: dict,
    adapter,
    all_backends: list[str],
) -> PolicyOutcome:
    """Process a PolicyUpdate payload.

    When safe_mode=True, revert to equal weights across all backends
    while preserving any existing exclusions (excluded backends stay down).
    """
    safe_mode = bool(payload.get("safe_mode", False))

    if not safe_mode:
        return PolicyOutcome(applied=False, safe_mode=False)

    try:
        equal_weights = {b: 1 for b in all_backends}
        adapter.set_upstream_weights(equal_weights)
        return PolicyOutcome(applied=True, safe_mode=True)
    except Exception as exc:  # noqa: BLE001
        return PolicyOutcome(applied=False, safe_mode=True, error=str(exc))
