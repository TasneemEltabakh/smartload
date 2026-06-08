"""
tests/unit/lb-sidecar/test_discover_all_backends.py
─────────────────────────────────────────────────────
Pure-Python unit tests for runloop.discover_all_backends + the cache.

Covers the #155 dynamic-pool foundation in the lb-sidecar:
  - Docker label query produces the expected sorted name:port list
  - TTL cache returns a stale snapshot until invalidation or expiry
  - Fallback to seed_backends on Docker error / empty result
  - Sorted by replica index (backend-2 before backend-10)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SERVICE = Path(__file__).resolve().parents[3] / "services" / "lb-sidecar"
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from runloop import (  # noqa: E402
    _BackendNameCache,
    _query_backends_via_docker,
    discover_all_backends,
    invalidate_backend_discovery_cache,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_running_container(name: str):
    """Mock a Docker container with the label runloop queries."""
    c = MagicMock()
    c.name = name
    c.status = "running"
    return c


def _make_docker_client(containers_list: list):
    client = MagicMock()
    client.containers.list = MagicMock(return_value=list(containers_list))
    return client


# ── _query_backends_via_docker ────────────────────────────────────────────────


class TestQueryBackends:
    def test_returns_sorted_name_port_list(self):
        # backend-10 must sort after backend-2 (replica index, not lexical).
        containers = [
            _make_running_container("smartload-test-backend-10"),
            _make_running_container("smartload-test-backend-2"),
            _make_running_container("smartload-test-backend-1"),
        ]
        client = _make_docker_client(containers)
        result = _query_backends_via_docker(client, fallback=[])
        assert result == [
            "smartload-test-backend-1:8080",
            "smartload-test-backend-2:8080",
            "smartload-test-backend-10:8080",
        ]

    def test_falls_back_on_empty_result(self):
        client = _make_docker_client([])
        fallback = ["smartload-test-backend-1:8080"]
        result = _query_backends_via_docker(client, fallback=fallback)
        assert result == fallback

    def test_falls_back_on_docker_error(self):
        client = MagicMock()
        client.containers.list = MagicMock(side_effect=RuntimeError("daemon down"))
        fallback = ["smartload-test-backend-1:8080"]
        result = _query_backends_via_docker(client, fallback=fallback)
        assert result == fallback

    def test_passes_through_when_docker_none(self):
        """Used during cold boot before the Docker client is built."""
        fallback = ["smartload-test-backend-1:8080"]
        result = _query_backends_via_docker(None, fallback=fallback)
        assert result == fallback

    def test_includes_only_test_backend_label(self):
        """The filter must specify the test-backend label so unrelated
        containers (the autoscaler itself, the lb-sidecar, etc.) never
        end up in upstream.conf."""
        containers = [_make_running_container("smartload-test-backend-1")]
        client = _make_docker_client(containers)
        _query_backends_via_docker(client, fallback=[])
        _args, kwargs = client.containers.list.call_args
        filters = kwargs["filters"]
        assert filters["label"] == "com.docker.compose.service=test-backend"
        assert filters["status"] == "running"


# ── _BackendNameCache (TTL semantics) ─────────────────────────────────────────


class TestBackendNameCache:
    def test_first_call_queries_docker(self):
        containers = [_make_running_container("smartload-test-backend-1")]
        client = _make_docker_client(containers)
        cache = _BackendNameCache(ttl_seconds=1.0)
        result = cache.get(client, fallback=[])
        assert result == ["smartload-test-backend-1:8080"]
        client.containers.list.assert_called_once()

    def test_second_call_within_ttl_returns_cached(self):
        containers = [_make_running_container("smartload-test-backend-1")]
        client = _make_docker_client(containers)
        cache = _BackendNameCache(ttl_seconds=5.0)
        cache.get(client, fallback=[])
        cache.get(client, fallback=[])
        # Only one Docker query — the second hit serves the cached snapshot.
        client.containers.list.assert_called_once()

    def test_call_after_ttl_requeries(self):
        containers = [_make_running_container("smartload-test-backend-1")]
        client = _make_docker_client(containers)
        cache = _BackendNameCache(ttl_seconds=0.05)
        cache.get(client, fallback=[])
        time.sleep(0.10)
        cache.get(client, fallback=[])
        assert client.containers.list.call_count == 2

    def test_invalidate_forces_requery(self):
        containers = [_make_running_container("smartload-test-backend-1")]
        client = _make_docker_client(containers)
        cache = _BackendNameCache(ttl_seconds=5.0)
        cache.get(client, fallback=[])
        cache.invalidate()
        cache.get(client, fallback=[])
        # invalidate() drops the cached snapshot — second call queries Docker.
        assert client.containers.list.call_count == 2

    def test_concurrent_set_changes_are_picked_up_after_invalidate(self):
        """A new backend provisioned mid-run is visible on the next discover()
        call after the cache has been invalidated. This is the live-update
        contract that the message-dispatch loop relies on."""
        before = [_make_running_container("smartload-test-backend-1")]
        after = before + [_make_running_container("smartload-test-backend-2")]
        client = MagicMock()
        client.containers.list = MagicMock(side_effect=[before, after])

        cache = _BackendNameCache(ttl_seconds=5.0)
        result_before = cache.get(client, fallback=[])
        assert result_before == ["smartload-test-backend-1:8080"]

        cache.invalidate()
        result_after = cache.get(client, fallback=[])
        assert result_after == [
            "smartload-test-backend-1:8080",
            "smartload-test-backend-2:8080",
        ]


# ── discover_all_backends (module-level wrapper) ──────────────────────────────


class TestDiscoverAllBackends:
    def test_uses_module_cache(self):
        # Two consecutive calls hit the module-level cache singleton.
        invalidate_backend_discovery_cache()
        containers = [_make_running_container("smartload-test-backend-1")]
        client = _make_docker_client(containers)
        a = discover_all_backends(client, seed_backends=[])
        b = discover_all_backends(client, seed_backends=[])
        assert a == b == ["smartload-test-backend-1:8080"]
        client.containers.list.assert_called_once()

    def test_invalidation_then_new_backend_visible(self):
        invalidate_backend_discovery_cache()
        before = [_make_running_container("smartload-test-backend-1")]
        after = before + [_make_running_container("smartload-test-backend-2")]
        client = MagicMock()
        client.containers.list = MagicMock(side_effect=[before, after])

        first = discover_all_backends(client, seed_backends=[])
        assert first == ["smartload-test-backend-1:8080"]
        invalidate_backend_discovery_cache()
        second = discover_all_backends(client, seed_backends=[])
        assert "smartload-test-backend-2:8080" in second

    def test_seed_used_when_docker_unavailable(self):
        invalidate_backend_discovery_cache()
        seed = ["smartload-test-backend-1:8080", "smartload-test-backend-2:8080"]
        result = discover_all_backends(docker_client=None, seed_backends=seed)
        assert result == seed
