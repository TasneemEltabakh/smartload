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
  - Initial state: compose creates `deploy.replicas` (5) test-backend
    containers, named smartload-test-backend-1..5. NGINX enumerates the
    same 5 hostnames in its upstream block.
  - "Active backends" = test-backend containers currently in `running`
    state. The total set of containers stays at 5; the autoscaler only
    toggles their running state.
  - scale_out → start the lowest-numbered stopped container.
  - scale_in  → stop the highest-numbered running container.

This keeps NGINX's static upstream block consistent across scale events.
proxy_next_upstream retries past whichever members are currently stopped.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

import docker

_BACKEND_LABEL_KEY   = "com.docker.compose.service"
_BACKEND_LABEL_VALUE = "test-backend"
_NUMBER_RE           = re.compile(r"-(\d+)$")


def _replica_number(name: str) -> int:
    """Extract the trailing replica index from a compose container name.

    `smartload-test-backend-3` → 3; an unparseable name sorts as 0 so it
    won't displace the numbered replicas in either direction.
    """
    match = _NUMBER_RE.search(name)
    return int(match.group(1)) if match else 0


class ClusterClient(ABC):
    """Minimal API the autoscaler needs from a container orchestrator."""

    @abstractmethod
    def get_backend_count(self) -> int:
        """Return the count of test-backend containers currently running."""

    @abstractmethod
    def scale_out(self) -> str | None:
        """Start one stopped backend. Return its name, or None if none stopped."""

    @abstractmethod
    def scale_in(self) -> str | None:
        """Stop one running backend. Return its name, or None if none running."""


class DockerClusterClient(ClusterClient):
    """Toggles compose-managed test-backend containers via Docker SDK."""

    def __init__(self, client: docker.DockerClient | None = None):
        self._client = client or docker.from_env()

    def _backends(self) -> list:
        """All test-backend containers (running + stopped), sorted by replica number."""
        containers = self._client.containers.list(
            all=True,
            filters={"label": f"{_BACKEND_LABEL_KEY}={_BACKEND_LABEL_VALUE}"},
        )
        return sorted(containers, key=lambda c: _replica_number(c.name))

    def get_backend_count(self) -> int:
        return sum(1 for c in self._backends() if c.status == "running")

    def scale_out(self) -> str | None:
        for container in self._backends():
            if container.status != "running":
                container.start()
                return container.name
        return None

    def scale_in(self) -> str | None:
        running = [c for c in self._backends() if c.status == "running"]
        if not running:
            return None
        target = running[-1]
        target.stop(timeout=5)
        return target.name
