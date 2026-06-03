"""
services/lb-sidecar/runloop.py
───────────────────────────────
Pure-Python pieces of the lb-sidecar run loop, separated from app.py so
they can be unit-tested without Flask, Redis, or a live Docker daemon.

app.py owns:
  - sockets and threads
  - live Docker client + Redis + TimescaleDB clients
  - Flask routes

This module owns:
  - BackendRegistry: Docker SDK IP→container_name mapping
  - scores_to_weights: RoutingRecommendation scores → NGINX integer weights
  - PolicyState: minimal policy snapshot the sidecar needs (safe_mode +
                 rl_confidence_threshold). Updated by every smartload.policy
                 envelope; consumed by handle_routing.
  - handle_routing / handle_anomaly / handle_policy: message dispatch
  - {Routing,Anomaly,Policy}Outcome dataclasses wrapping results

SOT-anchored rules (each call-site cites the originating SOT line):

  1. "RL never sees an UNHEALTHY backend in its action space. Policy
      safe_mode can short-circuit RL but never bypasses anomaly exclusion."
      — SOT §3.4 hierarchy line 1756 / §16 line 3699.
     => handle_routing merges RL-supplied weights with the full known
        backend pool so previously-excluded backends still appear in
        upstream.conf (rendered as `server … down;` by the adapter).
        Without the merge, a partial RL ranking would silently restore
        exclusions on every cycle.

  2. "rl_confidence_threshold: below this, sidecar ignores RL and uses
      classical." — SOT §13 line 3128.
     => handle_routing takes a confidence_threshold kwarg sourced from
        PolicyState; when max(scores) < threshold, the recommendation is
        rejected and the previous adapter state stands.

  3. "Shadow recommendations are logged but never applied." — SOT v1.0.6
      row line 5703.
     => Shadow mode produces an outcome with applied=False, mode="shadow"
        so app.py can emit a one-line log entry. Operators can confirm
        shadow envelopes are being received without an active rewrite.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
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
class PolicyState:
    """Minimal policy snapshot the sidecar needs.

    Tracked because the sidecar is not "no state" — it needs to know
    safe_mode (for the policy short-circuit) and rl_confidence_threshold
    (for the SOT §13 rule that low-confidence RL recommendations must be
    ignored). Updated by handle_policy on every smartload.policy publish.
    Defaults match the policy.yaml ship values; only fields that arrive on
    the wire are overwritten so a partial publish never wipes the live
    state.
    """
    safe_mode: bool = False
    rl_confidence_threshold: float = 0.0


@dataclass
class RoutingOutcome:
    """Result of processing one smartload.routing message."""
    applied: bool = False
    mode: str = "shadow"
    weight_count: int = 0
    error: Optional[str] = None
    # Confidence of the recommendation (max score). Surfaced so app.py can
    # log it alongside the apply / shadow / reject decision.
    confidence: float = 0.0
    rejected_below_threshold: bool = False


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
    rl_confidence_threshold: float = 0.0
    error: Optional[str] = None


# Floor weight assigned to known backends that aren't in the RL ranking
# (because the policy filtered them out as unhealthy/unknown). They still
# need to appear in upstream.conf so the adapter can render them — either
# as `down;` if anomaly-excluded, or with a tiny floor weight otherwise.
# The floor must be ≥ 1 because NGINX rejects weight=0; we use 1 so any
# omitted backend gets a near-zero share of traffic without disappearing.
KNOWN_BACKEND_FLOOR_WEIGHT: int = 1


def handle_routing(
    payload: dict,
    registry: BackendRegistry,
    adapter,
    all_backends: list[str],
    *,
    confidence_threshold: float = 0.0,
) -> RoutingOutcome:
    """Process a RoutingRecommendation payload.

    Behaviour by mode:
      "shadow"     — outcome with applied=False, mode="shadow" so the
                     caller can log the receipt. SOT v1.0.6 row line 5703:
                     "shadow envelopes received but not applied."
      "active"     — apply, subject to two gates:
                       (a) confidence = max(scores). If below
                           confidence_threshold (sourced from PolicyState,
                           ultimately from policy.yaml's
                           rl_confidence_threshold per SOT §13 line 3128),
                           reject the recommendation. Adapter state stands.
                       (b) merge with the full known-backend pool so any
                           backend not in the ranking still appears in
                           upstream.conf — preserving anomaly exclusions
                           per SOT §3.4 line 1756.
      anything else — treated as shadow.
    """
    mode = (payload.get("mode") or "shadow").lower()
    rankings = payload.get("server_rankings", [])
    scores = [float(r.get("score", 0.0)) for r in rankings]
    confidence = max(scores) if scores else 0.0

    if mode != "active":
        return RoutingOutcome(
            applied=False,
            mode=mode,
            weight_count=len(rankings),
            confidence=confidence,
        )

    # Confidence gate (SOT §13). Threshold of 0 ⇒ gate disabled.
    if confidence_threshold > 0.0 and confidence < confidence_threshold:
        return RoutingOutcome(
            applied=False,
            mode=mode,
            weight_count=len(rankings),
            confidence=confidence,
            rejected_below_threshold=True,
        )

    try:
        raw_weights = scores_to_weights(rankings)
        translated = registry.translate(raw_weights)
        # Merge into the full known pool (SOT §3.4 — anomaly exclusion
        # must survive a partial RL ranking). Start from a floor-weight
        # baseline so every known backend is represented; RL-supplied
        # weights override the floor where present.
        merged: dict[str, int] = {b: KNOWN_BACKEND_FLOOR_WEIGHT for b in all_backends}
        if not translated and all_backends:
            # Empty ranking on active envelope — fall through to the
            # equal-weight baseline (already set above).
            pass
        else:
            merged.update(translated)
        adapter.set_upstream_weights(merged)
        return RoutingOutcome(
            applied=True,
            mode=mode,
            weight_count=len(merged),
            confidence=confidence,
        )
    except Exception as exc:  # noqa: BLE001
        return RoutingOutcome(
            applied=False,
            mode=mode,
            error=str(exc),
            confidence=confidence,
        )


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
    *,
    policy_state: Optional[PolicyState] = None,
) -> PolicyOutcome:
    """Process a PolicyUpdate payload.

    Updates the in-memory PolicyState (if supplied) so subsequent
    handle_routing calls see the new safe_mode + rl_confidence_threshold.
    When safe_mode flips True, also reverts the adapter to equal weights
    across all backends while preserving any existing exclusions (the
    adapter renders excluded backends as `down;` regardless of weight).

    Missing fields fall back to the current PolicyState values so a
    partial publish never wipes live state.
    """
    fallback = policy_state if policy_state is not None else PolicyState()
    safe_mode = bool(payload.get("safe_mode", fallback.safe_mode))
    try:
        rl_threshold = float(
            payload.get("rl_confidence_threshold", fallback.rl_confidence_threshold)
        )
    except (TypeError, ValueError):
        rl_threshold = fallback.rl_confidence_threshold

    if policy_state is not None:
        policy_state.safe_mode = safe_mode
        policy_state.rl_confidence_threshold = rl_threshold

    if not safe_mode:
        return PolicyOutcome(
            applied=False,
            safe_mode=False,
            rl_confidence_threshold=rl_threshold,
        )

    try:
        equal_weights = {b: 1 for b in all_backends}
        adapter.set_upstream_weights(equal_weights)
        return PolicyOutcome(
            applied=True,
            safe_mode=True,
            rl_confidence_threshold=rl_threshold,
        )
    except Exception as exc:  # noqa: BLE001
        return PolicyOutcome(
            applied=False,
            safe_mode=True,
            rl_confidence_threshold=rl_threshold,
            error=str(exc),
        )
