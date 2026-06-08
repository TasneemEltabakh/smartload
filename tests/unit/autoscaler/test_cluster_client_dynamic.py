"""
tests/unit/autoscaler/test_cluster_client_dynamic.py
─────────────────────────────────────────────────────
Pure-Python unit tests for services/autoscaler/cluster_client.py
covering the #155 adaptive-bench additions:

  - provision() / decommission() — create and destroy containers via Docker SDK
  - _next_unused_index — lowest-unused index picker (1,2,4 → 3)
  - max_backends_ceiling — belt-and-braces ceiling enforcement
  - provisioning feature flag — provision() is a no-op when disabled
  - _wait_for_healthy — healthcheck-then-announce sequence (Risk 1 de-risk)
  - scale_out / scale_in tuple shape — (name, mechanism) return contract

No Docker daemon, no live containers — the test mocks the Docker SDK
surface (`docker.DockerClient`) via unittest.mock.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SERVICE = Path(__file__).resolve().parents[2].parent / "services" / "autoscaler"
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

# Make `docker` importable as a stub ONLY when the real SDK is not
# installed. The unit-tests CI job runs without docker-py and needs the
# stub; integration tests in the same pytest session need the real
# module so they can talk to the live Docker daemon. We must not poison
# `sys.modules["docker"]` if the real module is importable.
import types

try:
    import docker as _real_docker  # noqa: F401
except ImportError:
    docker_stub = types.ModuleType("docker")
    errors_stub = types.ModuleType("docker.errors")

    class _StubAPIError(Exception):
        pass

    class _StubNotFound(Exception):
        pass

    errors_stub.APIError = _StubAPIError
    errors_stub.NotFound = _StubNotFound
    docker_stub.errors = errors_stub
    docker_stub.from_env = lambda: MagicMock()
    docker_stub.DockerClient = MagicMock
    sys.modules["docker"] = docker_stub
    sys.modules["docker.errors"] = errors_stub

from cluster_client import (  # noqa: E402
    DockerClusterClient,
    _next_unused_index,
    _replica_number,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_container(
    name: str,
    *,
    status: str = "running",
    dynamic: bool = False,
    health_status: str | None = "healthy",
):
    """Build a mock Docker container with the attributes cluster_client reads."""
    c = MagicMock()
    c.name = name
    c.status = status
    c.labels = {"com.docker.compose.service": "test-backend"}
    if dynamic:
        c.labels["smartload.dynamic"] = "true"
    state = {"Status": status}
    if health_status is not None:
        state["Health"] = {"Status": health_status}
    c.attrs = {"State": state}

    # Make reload() update attrs from a queue so tests can simulate
    # healthcheck progression. Callers patch container.reload directly
    # when they need this; the default no-op is sufficient otherwise.
    def _reload():
        pass

    c.reload = _reload
    return c


def _make_docker_client(containers_list: list, run_returns=None):
    """Mock DockerClient that returns `containers_list` for containers.list()
    and `run_returns` (a single container) for containers.run()."""
    client = MagicMock()
    # containers.list — accept the kwargs cluster_client passes (all=, filters=)
    client.containers.list = MagicMock(return_value=list(containers_list))
    client.containers.run = MagicMock(return_value=run_returns)
    return client


# ── _next_unused_index ────────────────────────────────────────────────────────


class TestNextUnusedIndex:
    def test_empty_list_returns_one(self):
        assert _next_unused_index([]) == 1

    def test_sequential_picks_after_max(self):
        assert _next_unused_index([1, 2, 3]) == 4

    def test_picks_lowest_gap_not_after_max(self):
        # The critical case for #155: backends 1, 2, 4 exist (3 was
        # decommissioned). provision() must re-use slot 3, not jump to 5.
        assert _next_unused_index([1, 2, 4]) == 3

    def test_picks_first_when_zero_present(self):
        # `_replica_number` returns 0 for unparseable names; we never
        # produce backend_0 so the lowest used index is effectively 1+.
        assert _next_unused_index([0, 1, 2]) == 3

    def test_handles_unsorted_input(self):
        assert _next_unused_index([5, 1, 3, 2]) == 4


# ── _replica_number ───────────────────────────────────────────────────────────


class TestReplicaNumber:
    def test_extracts_trailing_index(self):
        assert _replica_number("smartload-test-backend-7") == 7

    def test_no_match_returns_zero(self):
        assert _replica_number("smartload-test-backend") == 0
        assert _replica_number("random-name") == 0


# ── feature flag ──────────────────────────────────────────────────────────────


class TestProvisioningFeatureFlag:
    def test_disabled_returns_none(self):
        """When the feature flag is OFF (the default), provision() never
        actuates and never calls containers.run. The legacy #148 routing
        bench harness depends on this — it imports DockerClusterClient
        without setting any provisioning kwargs and must see the same
        toggle-only behaviour as before."""
        client = _make_docker_client([])
        cluster = DockerClusterClient(client=client, provisioning_enabled=False)
        assert cluster.provision() is None
        client.containers.run.assert_not_called()

    def test_enabled_attempts_run(self):
        new_container = _make_container("smartload-test-backend-1", dynamic=True)
        client = _make_docker_client([], run_returns=new_container)
        cluster = DockerClusterClient(
            client=client, provisioning_enabled=True, healthcheck_timeout_seconds=2,
        )
        name = cluster.provision()
        assert name == "smartload-test-backend-1"
        client.containers.run.assert_called_once()


# ── containers.run args ──────────────────────────────────────────────────────


class TestProvisionArgs:
    def test_passes_image_name_network_labels(self):
        new_container = _make_container("smartload-test-backend-1", dynamic=True)
        client = _make_docker_client([], run_returns=new_container)
        cluster = DockerClusterClient(
            client=client,
            provisioning_enabled=True,
            provisioning_image="my-image:tagged",
            provisioning_network="my-net",
            healthcheck_timeout_seconds=2,
        )
        cluster.provision()
        # First positional arg is image; the rest are kwargs.
        args, kwargs = client.containers.run.call_args
        assert args[0] == "my-image:tagged"
        assert kwargs["name"] == "smartload-test-backend-1"
        assert kwargs["network"] == "my-net"
        assert kwargs["detach"] is True
        labels = kwargs["labels"]
        assert labels["com.docker.compose.service"] == "test-backend"
        assert labels["smartload.dynamic"] == "true"
        assert kwargs["restart_policy"]["Name"] == "unless-stopped"

    def test_index_allocation_picks_lowest_unused(self):
        # backends 1, 2, 4 already exist — provision() must produce backend-3.
        existing = [
            _make_container("smartload-test-backend-1"),
            _make_container("smartload-test-backend-2"),
            _make_container("smartload-test-backend-4"),
        ]
        new_container = _make_container("smartload-test-backend-3", dynamic=True)
        client = _make_docker_client(existing, run_returns=new_container)
        cluster = DockerClusterClient(
            client=client, provisioning_enabled=True, healthcheck_timeout_seconds=2,
        )
        cluster.provision()
        _args, kwargs = client.containers.run.call_args
        assert kwargs["name"] == "smartload-test-backend-3"


# ── ceiling enforcement ──────────────────────────────────────────────────────


class TestMaxBackendsCeiling:
    def test_provision_refused_at_ceiling(self):
        # max_backends_ceiling=3 with 3 labelled containers already → refuse.
        existing = [
            _make_container("smartload-test-backend-1"),
            _make_container("smartload-test-backend-2"),
            _make_container("smartload-test-backend-3"),
        ]
        client = _make_docker_client(existing)
        cluster = DockerClusterClient(
            client=client,
            provisioning_enabled=True,
            max_backends_ceiling=3,
            healthcheck_timeout_seconds=2,
        )
        assert cluster.provision() is None
        client.containers.run.assert_not_called()

    def test_provision_allowed_below_ceiling(self):
        existing = [_make_container("smartload-test-backend-1")]
        new_container = _make_container("smartload-test-backend-2", dynamic=True)
        client = _make_docker_client(existing, run_returns=new_container)
        cluster = DockerClusterClient(
            client=client,
            provisioning_enabled=True,
            max_backends_ceiling=10,
            healthcheck_timeout_seconds=2,
        )
        assert cluster.provision() == "smartload-test-backend-2"


# ── healthcheck wait (Risk 1 de-risk) ────────────────────────────────────────


class TestWaitForHealthy:
    def test_returns_immediately_when_healthy(self):
        container = _make_container("test-1", health_status="healthy")
        new_container = _make_container("smartload-test-backend-1", dynamic=True,
                                        health_status="healthy")
        client = _make_docker_client([], run_returns=new_container)
        cluster = DockerClusterClient(
            client=client, provisioning_enabled=True, healthcheck_timeout_seconds=5,
        )
        start = time.monotonic()
        name = cluster.provision()
        elapsed = time.monotonic() - start
        assert name == "smartload-test-backend-1"
        assert elapsed < 1.0   # Should be near-immediate

    def test_returns_none_on_timeout(self):
        # Container reports "starting" forever — provision() must time out.
        container = _make_container(
            "smartload-test-backend-1", dynamic=True, health_status="starting",
        )
        client = _make_docker_client([], run_returns=container)
        cluster = DockerClusterClient(
            client=client, provisioning_enabled=True, healthcheck_timeout_seconds=1,
        )
        start = time.monotonic()
        result = cluster.provision()
        elapsed = time.monotonic() - start
        assert result is None
        # Hits the timeout, doesn't loop forever
        assert elapsed >= 1.0
        assert elapsed < 3.0
        # On timeout, the container is stopped (not removed) for inspection.
        container.stop.assert_called()

    def test_no_healthcheck_treats_running_as_healthy(self):
        # Containers without a healthcheck spec fall back to State.Status == running.
        container = _make_container(
            "smartload-test-backend-1", dynamic=True, status="running",
            health_status=None,
        )
        client = _make_docker_client([], run_returns=container)
        cluster = DockerClusterClient(
            client=client, provisioning_enabled=True, healthcheck_timeout_seconds=2,
        )
        assert cluster.provision() == "smartload-test-backend-1"


# ── decommission ─────────────────────────────────────────────────────────────


class TestDecommission:
    def test_returns_none_when_no_dynamic(self):
        # Only compose-provisioned containers — decommission() must refuse.
        existing = [
            _make_container("smartload-test-backend-1"),
            _make_container("smartload-test-backend-2"),
        ]
        client = _make_docker_client(existing)
        cluster = DockerClusterClient(client=client, provisioning_enabled=True)
        assert cluster.decommission() is None

    def test_removes_highest_dynamic(self):
        # Mix: composes 1,2 + dynamics 3,4 — decommission() removes backend-4.
        compose_1 = _make_container("smartload-test-backend-1")
        compose_2 = _make_container("smartload-test-backend-2")
        dyn_3 = _make_container("smartload-test-backend-3", dynamic=True)
        dyn_4 = _make_container("smartload-test-backend-4", dynamic=True)
        client = _make_docker_client([compose_1, compose_2, dyn_3, dyn_4])
        cluster = DockerClusterClient(client=client, provisioning_enabled=True)
        assert cluster.decommission() == "smartload-test-backend-4"
        dyn_4.stop.assert_called_once()
        dyn_4.remove.assert_called_once_with(force=True)
        # Compose-provisioned containers are untouched.
        compose_1.stop.assert_not_called()
        compose_2.stop.assert_not_called()
        dyn_3.stop.assert_not_called()

    def test_never_removes_compose_provisioned(self):
        """Critical safety: the smartload.dynamic=true label is the gate.
        Even if a compose-provisioned container happens to share the
        backend label, decommission() must skip it."""
        compose_only = _make_container("smartload-test-backend-1")
        client = _make_docker_client([compose_only])
        cluster = DockerClusterClient(client=client, provisioning_enabled=True)
        assert cluster.decommission() is None
        compose_only.stop.assert_not_called()
        compose_only.remove.assert_not_called()


# ── scale_out / scale_in tuple shape ──────────────────────────────────────────


class TestScaleOutTuple:
    def test_returns_start_mechanism_when_stopped_exists(self):
        # A stopped container exists → start() path → mechanism="start".
        stopped = _make_container("smartload-test-backend-2", status="exited")
        running = _make_container("smartload-test-backend-1", status="running")
        client = _make_docker_client([running, stopped])
        cluster = DockerClusterClient(client=client, provisioning_enabled=True)
        result = cluster.scale_out()
        assert result == ("smartload-test-backend-2", "start")

    def test_falls_through_to_provision_when_no_stopped(self):
        # All running → start() returns None → provision() runs → mechanism="provision".
        running = _make_container("smartload-test-backend-1", status="running")
        new_container = _make_container("smartload-test-backend-2", dynamic=True)
        client = _make_docker_client([running], run_returns=new_container)
        cluster = DockerClusterClient(
            client=client, provisioning_enabled=True, healthcheck_timeout_seconds=2,
        )
        result = cluster.scale_out()
        assert result == ("smartload-test-backend-2", "provision")

    def test_returns_none_when_neither_path_actuates(self):
        # All running, provisioning OFF → both paths fail → return None.
        running = _make_container("smartload-test-backend-1", status="running")
        client = _make_docker_client([running])
        cluster = DockerClusterClient(client=client, provisioning_enabled=False)
        assert cluster.scale_out() is None


class TestScaleInTuple:
    def test_returns_decommission_mechanism_when_dynamic_exists(self):
        # A dynamic container exists → decommission() preferred over stop().
        compose_1 = _make_container("smartload-test-backend-1")
        dyn_2 = _make_container("smartload-test-backend-2", dynamic=True)
        client = _make_docker_client([compose_1, dyn_2])
        cluster = DockerClusterClient(client=client, provisioning_enabled=True)
        result = cluster.scale_in()
        assert result == ("smartload-test-backend-2", "decommission")
        # The compose backend stays running.
        compose_1.stop.assert_not_called()

    def test_falls_through_to_stop_when_no_dynamic(self):
        compose_1 = _make_container("smartload-test-backend-1", status="running")
        compose_2 = _make_container("smartload-test-backend-2", status="running")
        client = _make_docker_client([compose_1, compose_2])
        cluster = DockerClusterClient(client=client, provisioning_enabled=True)
        result = cluster.scale_in()
        # Highest-numbered compose backend is stopped.
        assert result == ("smartload-test-backend-2", "stop")

    def test_returns_none_when_pool_empty(self):
        client = _make_docker_client([])
        cluster = DockerClusterClient(client=client, provisioning_enabled=True)
        assert cluster.scale_in() is None


# ── get_backend_count ────────────────────────────────────────────────────────


class TestGetBackendCount:
    def test_counts_only_running(self):
        containers = [
            _make_container("smartload-test-backend-1", status="running"),
            _make_container("smartload-test-backend-2", status="exited"),
            _make_container("smartload-test-backend-3", status="running"),
        ]
        client = _make_docker_client(containers)
        cluster = DockerClusterClient(client=client)
        assert cluster.get_backend_count() == 2

    def test_counts_dynamic_and_compose_together(self):
        # The count is a single "running test-backend" number regardless of
        # provisioning mechanism — the lb-sidecar sees one pool.
        containers = [
            _make_container("smartload-test-backend-1", status="running"),
            _make_container("smartload-test-backend-2", status="running", dynamic=True),
        ]
        client = _make_docker_client(containers)
        cluster = DockerClusterClient(client=client)
        assert cluster.get_backend_count() == 2
