"""
tests/integration/test_autoscaler_dynamic_pool.py
──────────────────────────────────────────────────
Integration tests for the #155 adaptive-bench dynamic-pool foundation.

These tests exercise services/autoscaler/cluster_client.py against the
LIVE Docker daemon — they create real containers, wait for the real
healthcheck, and tear them down.

Requirements:
  - docker-compose stack up (so the smartload-test-backend image is
    present and the smartload_smartload-net network exists)
  - Docker socket reachable from the test runner

The DockerClusterClient is instantiated directly with
provisioning_enabled=True — we do NOT need to reconfigure the
autoscaler container for these tests; we exercise the cluster-client
contract in isolation. The autoscaler's `apply_decision` integration
is covered by test_autoscaler.py.

Risk 1 de-risk (#155): the test asserts that provision() returns only
AFTER the new container's healthcheck reports `healthy`. This is the
ordering that prevents the lb-sidecar from rewriting upstream.conf
with a hostname Docker DNS hasn't propagated yet.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import docker as docker_sdk
import pytest

# Bring the autoscaler service onto sys.path so we can import cluster_client.
_AUTOSCALER = Path(__file__).resolve().parents[2] / "services" / "autoscaler"
if str(_AUTOSCALER) not in sys.path:
    sys.path.insert(0, str(_AUTOSCALER))

from cluster_client import (  # noqa: E402
    DockerClusterClient,
    _replica_number,
)

# `stack_ready` is auto-discovered from tests/integration/conftest.py by
# pytest's fixture resolution — no explicit import needed. The
# repo's tests are namespace packages (no __init__.py since v1.0.7a),
# so relative imports like `from .conftest import stack_ready` raise
# ImportError; the fixture-resolution path is the canonical way to
# consume conftest objects in this layout.


# ── helpers ───────────────────────────────────────────────────────────────────


_BACKEND_LABEL_FILTER = {
    "label": "com.docker.compose.service=test-backend",
}


def _list_dynamic_containers(docker_client) -> list:
    """Return any test-backend container carrying the dynamic label."""
    containers = docker_client.containers.list(
        all=True, filters=_BACKEND_LABEL_FILTER,
    )
    return [
        c for c in containers
        if (c.labels or {}).get("smartload.dynamic") == "true"
    ]


def _cleanup_dynamic_pool(docker_client) -> None:
    """Remove any lingering dynamic containers from prior runs.

    The bench's own teardown runs this — but the integration tests act
    as a belt-and-braces guard so a flaky run doesn't pollute the next
    test's view of the pool."""
    for c in _list_dynamic_containers(docker_client):
        try:
            if c.status == "running":
                c.stop(timeout=5)
            c.remove(force=True)
        except docker_sdk.errors.APIError:
            pass


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def docker_client(stack_ready):
    client = docker_sdk.from_env()
    yield client
    client.close()


@pytest.fixture(scope="function")
def clean_dynamic_pool(docker_client):
    """Ensure no dynamic containers exist before AND after each test."""
    _cleanup_dynamic_pool(docker_client)
    yield
    _cleanup_dynamic_pool(docker_client)


@pytest.fixture(scope="function")
def cluster(docker_client, clean_dynamic_pool):
    """A DockerClusterClient with provisioning enabled, for tests that
    exercise create/destroy directly. The clean_dynamic_pool fixture
    guarantees no leftover dynamic containers from prior runs."""
    return DockerClusterClient(
        client=docker_client,
        provisioning_enabled=True,
        provisioning_image="smartload-test-backend:latest",
        provisioning_network="smartload_smartload-net",
        max_backends_ceiling=10,
        healthcheck_timeout_seconds=45,  # CI hosts can be slow
    )


# ── tests ─────────────────────────────────────────────────────────────────────


class TestProvisionRoundtrip:

    def test_provision_returns_only_after_healthy(self, cluster, docker_client):
        """Risk 1 de-risk — provision() must not return until the
        container reports healthy. This is what makes it safe for the
        autoscaler to publish a ScalingEvent on the returned name (the
        lb-sidecar will rewrite upstream.conf and `nginx -s reload` will
        succeed because Docker DNS has had time to propagate)."""
        name = cluster.provision()
        assert name is not None
        assert name.startswith("smartload-test-backend-")

        # By the time provision() returned, the container is healthy.
        container = docker_client.containers.get(name)
        container.reload()
        health = container.attrs.get("State", {}).get("Health", {})
        assert health.get("Status") == "healthy", (
            f"container {name} reported healthy=False after provision() returned — "
            "the Risk-1 ordering is broken; the autoscaler must not publish until "
            "the container is actually healthy."
        )

    def test_provision_decommission_cycle(self, cluster, docker_client):
        """Full lifecycle: provision a new backend, count it, decommission,
        verify it's gone."""
        before_count = cluster.get_backend_count()
        name = cluster.provision()
        assert name is not None
        after_provision_count = cluster.get_backend_count()
        assert after_provision_count == before_count + 1

        removed = cluster.decommission()
        assert removed == name

        # Wait briefly for Docker's view to reflect the removal.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            current = cluster.get_backend_count()
            if current == before_count:
                break
            time.sleep(0.5)
        else:
            pytest.fail(
                f"backend count did not return to {before_count} after decommission()"
            )

    def test_provisioned_container_carries_dynamic_label(self, cluster, docker_client):
        """The smartload.dynamic=true label is the safety contract that
        prevents decommission() from ever tearing down a
        compose-provisioned container."""
        name = cluster.provision()
        assert name is not None

        container = docker_client.containers.get(name)
        assert container.labels.get("smartload.dynamic") == "true"
        assert container.labels.get("com.docker.compose.service") == "test-backend"

    def test_provision_picks_lowest_unused_index(self, cluster, docker_client):
        """If backends 1..5 exist (compose-provisioned), the first
        provisioned container is backend-6 (the lowest unused integer)."""
        before_indices = sorted(
            _replica_number(c.name)
            for c in docker_client.containers.list(
                all=True, filters=_BACKEND_LABEL_FILTER,
            )
        )
        name = cluster.provision()
        assert name is not None
        new_index = _replica_number(name)
        assert new_index not in before_indices
        assert new_index == max(before_indices) + 1, (
            f"expected backend-{max(before_indices) + 1}; got {name}"
        )


class TestProvisioningFeatureFlag:

    def test_disabled_blocks_provision(self, docker_client, clean_dynamic_pool):
        """When provisioning is disabled, provision() must be a no-op.
        The #148 routing bench harness depends on this — instantiating
        DockerClusterClient without the kwarg uses the default
        (provisioning_enabled=False) and behaves exactly as it did
        before #155 landed."""
        cluster = DockerClusterClient(
            client=docker_client, provisioning_enabled=False,
        )
        before_count = len(_list_dynamic_containers(docker_client))
        assert cluster.provision() is None
        after_count = len(_list_dynamic_containers(docker_client))
        assert after_count == before_count

    def test_scale_out_falls_back_to_start_when_provisioning_off(
        self, docker_client, clean_dynamic_pool,
    ):
        """Backwards-compat: when there's a stopped compose-provisioned
        container and provisioning is off, scale_out() runs the legacy
        start() path and returns ('name', 'start')."""
        cluster_legacy = DockerClusterClient(
            client=docker_client, provisioning_enabled=False,
        )

        # Manufacture a known starting state: stop one compose backend so
        # there's a target for start().
        stopped_one = False
        all_compose = docker_client.containers.list(
            all=True,
            filters={
                "label": "com.docker.compose.service=test-backend",
            },
        )
        # Pick a non-dynamic, currently-running backend to stop.
        for c in sorted(all_compose, key=lambda x: _replica_number(x.name), reverse=True):
            labels = c.labels or {}
            if labels.get("smartload.dynamic") == "true":
                continue
            if c.status == "running":
                c.stop(timeout=5)
                stopped_one = True
                stopped_name = c.name
                break

        if not stopped_one:
            pytest.skip("no compose-provisioned backend available to stop")

        try:
            result = cluster_legacy.scale_out()
            assert result is not None
            name, mechanism = result
            assert mechanism == "start", (
                f"expected scale_out to use start() mechanism with provisioning off; got {mechanism}"
            )
            assert name == stopped_name
        finally:
            # Restore the stopped backend to a running state for the next test.
            docker_client.containers.get(stopped_name).start()


class TestCeilingEnforcement:

    def test_provision_refused_beyond_ceiling(self, docker_client, clean_dynamic_pool):
        """The cluster-client's max_backends_ceiling is the belt-and-braces
        guard. Even if decisions.py is bypassed (e.g. by a manual scale
        call from the operator UI), provision() refuses to grow the
        labelled-container count past the ceiling."""
        current = len(docker_client.containers.list(
            all=True,
            filters={
                "label": "com.docker.compose.service=test-backend",
            },
        ))
        cluster = DockerClusterClient(
            client=docker_client,
            provisioning_enabled=True,
            max_backends_ceiling=current,   # already at ceiling
            healthcheck_timeout_seconds=10,
        )
        assert cluster.provision() is None
