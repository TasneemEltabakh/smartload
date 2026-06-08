"""
services/autoscaler/cluster_client.py
──────────────────────────────────────
Cluster abstraction. The autoscaler's business logic talks to this surface,
not to Docker directly — so a KubernetesClusterClient can drop in later
without rewriting decisions.py or app.py.

SOT §8.8 review checklist item:
    "Does the Docker abstraction allow swapping to K8s API without
     rewriting business logic?"

Scaling model (prototype, single host):
  Two distinct lifecycle pairs share the same business-logic surface:

  - `start()` / `stop()` toggle the running state of a container that
    already exists. Compose provisions the initial pool (5 containers
    named `smartload-test-backend-1..5`); the autoscaler toggles their
    `running` flag to express scale-out / scale-in within that fixed set.
    This is the only path used by the legacy #148 routing bench harness.

  - `provision()` / `decommission()` create and destroy containers from
    the same `test-backend` image. Used by the #155 adaptive bench
    harness, which needs to grow the pool past compose's initial set
    (`min_backends=1` up to `max_backends=8+`). Dynamically-created
    containers carry an extra label `smartload.dynamic=true` so the
    decommission path can never tear down compose-provisioned containers.

`scale_out()` / `scale_in()` are the canonical autoscaler entry points;
they pick the right lifecycle pair internally based on the cluster's
current state + the `provisioning_enabled` flag.

NGINX integration:
  The lb-sidecar discovers backends via Docker label query on each cycle
  (`runloop.discover_all_backends`), so a newly-provisioned container is
  added to `upstream.conf` automatically on the sidecar's next refresh.
  The autoscaler waits for the container's healthcheck to report
  `healthy` before announcing the action — so by the time the sidecar
  sees the new backend, NGINX can resolve its hostname.

Future Kubernetes shim:
  The ABC now carries `provision()` / `decommission()` so a
  `KubernetesClusterClient` (issue #133 Helm chart workstream) inherits
  the full contract obligation. The Docker label semantics translate to
  K8s pod-template labels + deployment replica edits.
"""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod

import docker

_BACKEND_LABEL_KEY        = "com.docker.compose.service"
_BACKEND_LABEL_VALUE      = "test-backend"
_DYNAMIC_LABEL_KEY        = "smartload.dynamic"
_DYNAMIC_LABEL_VALUE      = "true"
_NUMBER_RE                = re.compile(r"-(\d+)$")
_NAME_PREFIX              = "smartload-test-backend"
_DEFAULT_BACKEND_PORT     = 8080
_DEFAULT_HEALTHCHECK_TIMEOUT_SECONDS = 30
_HEALTHCHECK_POLL_INTERVAL_SECONDS   = 1.0

# Healthcheck spec injected into provisioned containers. Mirrors the compose
# healthcheck on the test-backend service so dynamic backends report healthy
# state via Docker's healthcheck pipeline — the same `State.Health.Status`
# field the `_wait_for_healthy` poller reads. The Docker SDK takes durations
# in nanoseconds (consistent with the Engine API contract).
_NANOS_PER_SECOND = 1_000_000_000
_BACKEND_HEALTHCHECK = {
    "test":         ["CMD-SHELL", "wget -q -O /dev/null http://localhost:8080/ || exit 1"],
    "interval":     5 * _NANOS_PER_SECOND,
    "timeout":      3 * _NANOS_PER_SECOND,
    "retries":      5,
    "start_period": 5 * _NANOS_PER_SECOND,
}

# Default network name when compose project is "smartload". The actual
# runtime network name is <project>_<network>; the override env var
# AUTOSCALER_PROVISION_NETWORK is the operator escape hatch.
_DEFAULT_NETWORK = "smartload_smartload-net"

# Default image tag the bench-time dynamic provisioning uses. Operators
# can override per-deploy via AUTOSCALER_PROVISION_IMAGE.
_DEFAULT_IMAGE = "smartload-test-backend:latest"

log = logging.getLogger("autoscaler.cluster_client")


def _replica_number(name: str) -> int:
    """Extract the trailing replica index from a compose container name.

    `smartload-test-backend-3` → 3; an unparseable name sorts as 0 so it
    won't displace the numbered replicas in either direction.
    """
    match = _NUMBER_RE.search(name)
    return int(match.group(1)) if match else 0


def _next_unused_index(existing_indices: list[int]) -> int:
    """Return the lowest integer ≥ 1 not in `existing_indices`.

    Picks 3 when 1, 2, 4 exist — so decommissioning a middle backend and
    later provisioning a replacement re-uses the freed slot rather than
    monotonically inflating the index. Keeps container names bounded.
    """
    in_use = set(existing_indices)
    n = 1
    while n in in_use:
        n += 1
    return n


class ClusterClient(ABC):
    """Minimal API the autoscaler needs from a container orchestrator.

    Two lifecycle pairs are exposed so callers can distinguish "toggle
    running state on an already-existing container" from "create a new
    container from scratch." See module docstring.
    """

    @abstractmethod
    def get_backend_count(self) -> int:
        """Return the count of test-backend containers currently running."""

    @abstractmethod
    def start(self) -> str | None:
        """Start one stopped backend. Return its name, or None if none stopped."""

    @abstractmethod
    def stop(self) -> str | None:
        """Stop one running backend. Return its name, or None if none running."""

    @abstractmethod
    def provision(self) -> str | None:
        """Create a new backend container. Return its name once it reports
        healthy, or None if provisioning is disabled / capped / failed."""

    @abstractmethod
    def decommission(self) -> str | None:
        """Stop + remove a dynamically-created backend (one carrying the
        `smartload.dynamic=true` label). Return its name, or None if no
        dynamic container is currently running."""

    @abstractmethod
    def scale_out(self) -> tuple[str, str] | None:
        """Add one backend. Prefers `start()` over `provision()` so the
        cheap path runs when a stopped labelled container is available.

        Returns (container_name, mechanism) where mechanism is one of
        "start" | "provision", or None if no backend could be added
        (no stopped labelled container AND provisioning disabled / capped /
        failed)."""

    @abstractmethod
    def scale_in(self) -> tuple[str, str] | None:
        """Remove one backend. Prefers `decommission()` over `stop()`
        when a dynamic container exists, so the dynamic pool returns to
        its compose-provisioned floor cleanly.

        Returns (container_name, mechanism) where mechanism is one of
        "stop" | "decommission", or None if no backend could be removed."""


class DockerClusterClient(ClusterClient):
    """Toggles + provisions compose-managed test-backend containers via Docker SDK.

    Args:
        client: an existing ``docker.DockerClient`` (or a mock in tests).
            Defaults to ``docker.from_env()`` when omitted.
        provisioning_enabled: feature flag. When False (the default), only
            the legacy `start()` / `stop()` paths actuate; `provision()` and
            `decommission()` return None and log a one-line warning. Wire
            this from policy.yaml's `provisioning.enabled` field.
        provisioning_image: image tag used by `provision()`.
        provisioning_network: Docker network the new container joins.
        max_backends_ceiling: belt-and-braces guard. Even if `decisions.py`
            is bypassed, `provision()` refuses to push the labelled-container
            count above this number.
        healthcheck_timeout_seconds: how long `provision()` polls for the
            new container's healthcheck to report `healthy` before giving up.
    """

    def __init__(
        self,
        client: docker.DockerClient | None = None,
        *,
        provisioning_enabled: bool = False,
        provisioning_image: str = _DEFAULT_IMAGE,
        provisioning_network: str = _DEFAULT_NETWORK,
        max_backends_ceiling: int = 10,
        healthcheck_timeout_seconds: int = _DEFAULT_HEALTHCHECK_TIMEOUT_SECONDS,
    ):
        self._client = client or docker.from_env()
        self._provisioning_enabled = provisioning_enabled
        self._provisioning_image   = provisioning_image
        self._provisioning_network = provisioning_network
        self._max_backends_ceiling = max_backends_ceiling
        self._healthcheck_timeout  = healthcheck_timeout_seconds

    # ── shared helpers ────────────────────────────────────────────────────

    def _backends(self) -> list:
        """All test-backend containers (running + stopped), sorted by replica number."""
        containers = self._client.containers.list(
            all=True,
            filters={"label": f"{_BACKEND_LABEL_KEY}={_BACKEND_LABEL_VALUE}"},
        )
        return sorted(containers, key=lambda c: _replica_number(c.name))

    def _dynamic_backends(self) -> list:
        """Subset of `_backends()` that carry the dynamic-provisioning label."""
        return [
            c for c in self._backends()
            if (c.labels or {}).get(_DYNAMIC_LABEL_KEY) == _DYNAMIC_LABEL_VALUE
        ]

    def get_backend_count(self) -> int:
        return sum(1 for c in self._backends() if c.status == "running")

    # ── pair 1: start / stop (toggle existing) ────────────────────────────

    def start(self) -> str | None:
        for container in self._backends():
            if container.status != "running":
                container.start()
                return container.name
        return None

    def stop(self) -> str | None:
        running = [c for c in self._backends() if c.status == "running"]
        if not running:
            return None
        target = running[-1]
        target.stop(timeout=5)
        return target.name

    # ── pair 2: provision / decommission (create / destroy) ───────────────

    def provision(self) -> str | None:
        """Create a new test-backend container and wait for healthy.

        Returns the new container's name once `container.attrs["State"]["Health"]["Status"]`
        reads `healthy`. Returns None when:
          - the provisioning feature flag is off (returns None silently);
          - the labelled-container count is already at `max_backends_ceiling`;
          - the healthcheck timeout elapses without the container reporting healthy;
          - the Docker SDK call raises.

        On healthcheck timeout the container is left in place (stopped, but
        not removed) so an operator can investigate why it failed to start.
        """
        if not self._provisioning_enabled:
            log.warning("provision() called but provisioning.enabled is False — ignoring")
            return None

        all_backends = self._backends()
        if len(all_backends) >= self._max_backends_ceiling:
            log.warning(
                "provision() refused — labelled container count %d at ceiling %d",
                len(all_backends), self._max_backends_ceiling,
            )
            return None

        index = _next_unused_index([_replica_number(c.name) for c in all_backends])
        name  = f"{_NAME_PREFIX}-{index}"

        try:
            container = self._client.containers.run(
                self._provisioning_image,
                name=name,
                network=self._provisioning_network,
                labels={
                    _BACKEND_LABEL_KEY:  _BACKEND_LABEL_VALUE,
                    _DYNAMIC_LABEL_KEY:  _DYNAMIC_LABEL_VALUE,
                    "smartload.role":    "test-backend",
                },
                environment={"PORT": str(_DEFAULT_BACKEND_PORT)},
                healthcheck=_BACKEND_HEALTHCHECK,
                restart_policy={"Name": "unless-stopped"},
                detach=True,
            )
        except docker.errors.APIError as exc:
            log.error("provision(%s) failed at containers.run(): %s", name, exc)
            return None

        if not self._wait_for_healthy(container):
            log.error(
                "provision(%s) timed out waiting for healthy after %ds — "
                "leaving the container in place for inspection",
                name, self._healthcheck_timeout,
            )
            try:
                container.stop(timeout=5)
            except docker.errors.APIError:
                pass
            return None

        log.info("provision(%s) healthy", name)
        return name

    def decommission(self) -> str | None:
        """Stop + remove the highest-indexed dynamic container.

        Only containers carrying `smartload.dynamic=true` are eligible —
        the label gate is the safety contract that prevents this method
        from ever tearing down a compose-provisioned container.
        """
        dynamic_running = [
            c for c in self._dynamic_backends() if c.status == "running"
        ]
        if not dynamic_running:
            return None

        target = dynamic_running[-1]
        name = target.name
        try:
            target.stop(timeout=5)
            target.remove(force=True)
        except docker.errors.APIError as exc:
            log.error("decommission(%s) failed: %s", name, exc)
            return None
        return name

    # ── canonical autoscaler entry points ─────────────────────────────────

    def scale_out(self) -> tuple[str, str] | None:
        """Add one backend.

        Prefers `start()` when a stopped labelled container exists (cheap
        path — no image pull, no healthcheck wait). Falls through to
        `provision()` only when the stopped pool is empty AND provisioning
        is enabled. The belt-and-braces ceiling guard inside `provision()`
        is the second layer of `max_backends` enforcement; the primary
        gate is `decisions.py`.

        Returns (name, "start") or (name, "provision"), or None when
        neither path actuated.
        """
        started = self.start()
        if started is not None:
            return started, "start"
        provisioned = self.provision()
        if provisioned is not None:
            return provisioned, "provision"
        return None

    def scale_in(self) -> tuple[str, str] | None:
        """Remove one backend.

        Prefers `decommission()` when a dynamic container is running, so the
        pool returns to its compose-provisioned floor cleanly before any
        compose-provisioned container is stopped. Falls through to `stop()`
        when no dynamic container exists.

        Returns (name, "decommission") or (name, "stop"), or None when
        neither path actuated.
        """
        decommissioned = self.decommission()
        if decommissioned is not None:
            return decommissioned, "decommission"
        stopped = self.stop()
        if stopped is not None:
            return stopped, "stop"
        return None

    # ── internal: healthcheck poll ────────────────────────────────────────

    def _wait_for_healthy(self, container) -> bool:
        """Poll container State.Health.Status until `healthy` or timeout.

        Returns True iff `healthy` is reached within the timeout. Returns
        False on:
          - timeout (typically because the healthcheck command itself
            takes too long, the image's CMD failed, or the container is
            stuck initialising);
          - the container disappearing (someone else removed it during
            the poll).

        Containers without a healthcheck spec return `State.Health.Status`
        = absent; in that case we fall back to checking the container is
        in the `running` state for at least one poll interval.
        """
        deadline = time.monotonic() + self._healthcheck_timeout
        has_healthcheck: bool | None = None
        while time.monotonic() < deadline:
            try:
                container.reload()
            except docker.errors.NotFound:
                return False
            state = container.attrs.get("State", {}) or {}
            health = state.get("Health") or {}
            status = health.get("Status")
            if has_healthcheck is None:
                has_healthcheck = bool(health)
            if has_healthcheck and status == "healthy":
                return True
            if (not has_healthcheck) and state.get("Status") == "running":
                # No healthcheck defined — treat one consecutive `running`
                # observation as healthy. The compose-time healthcheck is
                # in the image; this branch is a defensive path for images
                # built without one.
                return True
            time.sleep(_HEALTHCHECK_POLL_INTERVAL_SECONDS)
        return False
