"""
tests/conformance/lb_adapter/test_conformance.py
─────────────────────────────────────────────────
Conformance tests for the LoadBalancerAdapter contract.

These tests are written against the LoadBalancerAdapter ABC and run against:
  - A minimal MockAdapter (validates the harness itself)
  - The real NginxAdapter (with tmp conf dir + mock docker exec)

Contract properties tested:
  1. set_upstream_weights — idempotent; state reflects new weights.
  2. exclude_backend — idempotent; excluded backend appears in state.
  3. include_backend — idempotent; restores backend.
  4. current_state — always returns a consistent AdapterState snapshot.
  5. All-excluded fallback — adapter does not crash; state is consistent.
  6. weight=0 is not written as weight=0 (use down or floor to 1).
  7. set_upstream_weights does not un-exclude already-excluded backends.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SERVICES = Path(__file__).resolve().parents[3] / "services"
if str(_SERVICES) not in sys.path:
    sys.path.insert(0, str(_SERVICES))

from shared.lb_adapters.base import AdapterState, LoadBalancerAdapter  # noqa: E402
from shared.lb_adapters.nginx import NginxAdapter                      # noqa: E402


# ── Minimal mock adapter for harness validation ───────────────────────────────

class MockAdapter(LoadBalancerAdapter):
    def __init__(self):
        self._weights: dict[str, int] = {}
        self._excluded: set[str] = set()

    def set_upstream_weights(self, backend_weights: dict[str, int]) -> None:
        if backend_weights == self._weights:
            return
        self._weights = dict(backend_weights)

    def exclude_backend(self, backend_id: str) -> None:
        self._excluded.add(backend_id)

    def include_backend(self, backend_id: str) -> None:
        self._excluded.discard(backend_id)

    def current_state(self) -> AdapterState:
        return AdapterState(
            upstream_weights=dict(self._weights),
            excluded_backends=set(self._excluded),
        )


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_nginx_docker():
    docker = MagicMock()
    container = MagicMock()
    container.exec_run.return_value = (0, b"")
    docker.containers.get.return_value = container
    return docker


@pytest.fixture
def mock_adapter():
    return MockAdapter()


@pytest.fixture
def nginx_adapter(tmp_path):
    docker = _make_nginx_docker()
    return NginxAdapter(
        conf_path=tmp_path / "upstream.conf",
        nginx_container="lb",
        docker_client=docker,
        all_backends=["b1:8080", "b2:8080", "b3:8080"],
    )


ADAPTERS = ["mock_adapter", "nginx_adapter"]


# ── Conformance tests (parameterised over both adapters) ──────────────────────

@pytest.mark.parametrize("adapter_fixture", ADAPTERS)
def test_set_upstream_weights_idempotent(adapter_fixture, request):
    adapter = request.getfixturevalue(adapter_fixture)
    weights = {"b1:8080": 80, "b2:8080": 60}
    adapter.set_upstream_weights(weights)
    state_before = adapter.current_state()
    adapter.set_upstream_weights(weights)  # second call — must be no-op
    state_after = adapter.current_state()
    assert state_before.upstream_weights == state_after.upstream_weights


@pytest.mark.parametrize("adapter_fixture", ADAPTERS)
def test_set_upstream_weights_state_reflects_new_weights(adapter_fixture, request):
    adapter = request.getfixturevalue(adapter_fixture)
    adapter.set_upstream_weights({"b1:8080": 90, "b2:8080": 10})
    state = adapter.current_state()
    assert state.upstream_weights["b1:8080"] == 90
    assert state.upstream_weights["b2:8080"] == 10


@pytest.mark.parametrize("adapter_fixture", ADAPTERS)
def test_exclude_backend_idempotent(adapter_fixture, request):
    adapter = request.getfixturevalue(adapter_fixture)
    adapter.set_upstream_weights({"b1:8080": 70, "b2:8080": 50})
    adapter.exclude_backend("b1:8080")
    state_before = adapter.current_state()
    adapter.exclude_backend("b1:8080")  # second call — must be no-op
    state_after = adapter.current_state()
    assert state_before.excluded_backends == state_after.excluded_backends


@pytest.mark.parametrize("adapter_fixture", ADAPTERS)
def test_exclude_backend_appears_in_state(adapter_fixture, request):
    adapter = request.getfixturevalue(adapter_fixture)
    adapter.set_upstream_weights({"b1:8080": 60})
    adapter.exclude_backend("b1:8080")
    assert "b1:8080" in adapter.current_state().excluded_backends


@pytest.mark.parametrize("adapter_fixture", ADAPTERS)
def test_include_backend_idempotent(adapter_fixture, request):
    adapter = request.getfixturevalue(adapter_fixture)
    adapter.include_backend("b1:8080")  # not excluded — must be no-op
    adapter.include_backend("b1:8080")  # still not excluded — no-op again
    assert "b1:8080" not in adapter.current_state().excluded_backends


@pytest.mark.parametrize("adapter_fixture", ADAPTERS)
def test_include_backend_restores(adapter_fixture, request):
    adapter = request.getfixturevalue(adapter_fixture)
    adapter.set_upstream_weights({"b1:8080": 80})
    adapter.exclude_backend("b1:8080")
    assert "b1:8080" in adapter.current_state().excluded_backends
    adapter.include_backend("b1:8080")
    assert "b1:8080" not in adapter.current_state().excluded_backends


@pytest.mark.parametrize("adapter_fixture", ADAPTERS)
def test_current_state_is_consistent(adapter_fixture, request):
    adapter = request.getfixturevalue(adapter_fixture)
    adapter.set_upstream_weights({"b1:8080": 90, "b2:8080": 70, "b3:8080": 50})
    adapter.exclude_backend("b3:8080")
    state = adapter.current_state()
    assert isinstance(state, AdapterState)
    assert isinstance(state.upstream_weights, dict)
    assert isinstance(state.excluded_backends, set)
    assert "b3:8080" in state.excluded_backends


@pytest.mark.parametrize("adapter_fixture", ADAPTERS)
def test_all_excluded_does_not_crash(adapter_fixture, request):
    adapter = request.getfixturevalue(adapter_fixture)
    adapter.set_upstream_weights({"b1:8080": 1, "b2:8080": 1})
    adapter.exclude_backend("b1:8080")
    adapter.exclude_backend("b2:8080")
    state = adapter.current_state()
    assert state.excluded_backends == {"b1:8080", "b2:8080"}


@pytest.mark.parametrize("adapter_fixture", ADAPTERS)
def test_set_upstream_weights_does_not_unexclude(adapter_fixture, request):
    adapter = request.getfixturevalue(adapter_fixture)
    adapter.set_upstream_weights({"b1:8080": 80, "b2:8080": 60})
    adapter.exclude_backend("b2:8080")
    adapter.set_upstream_weights({"b1:8080": 90, "b2:8080": 70})
    # b2 should still be excluded after weight update
    assert "b2:8080" in adapter.current_state().excluded_backends


# ── NginxAdapter-specific: conf file content ──────────────────────────────────

def test_nginx_weight_zero_not_written(tmp_path):
    adapter = NginxAdapter(
        conf_path=tmp_path / "upstream.conf",
        nginx_container="lb",
        docker_client=_make_nginx_docker(),
    )
    adapter.set_upstream_weights({"b1:8080": 0})
    content = (tmp_path / "upstream.conf").read_text()
    assert "weight=0" not in content


def test_nginx_excluded_backend_uses_down_directive(tmp_path):
    adapter = NginxAdapter(
        conf_path=tmp_path / "upstream.conf",
        nginx_container="lb",
        docker_client=_make_nginx_docker(),
        all_backends=["b1:8080"],
    )
    adapter.set_upstream_weights({"b1:8080": 80})
    adapter.exclude_backend("b1:8080")
    content = (tmp_path / "upstream.conf").read_text()
    assert "b1:8080 down" in content
    assert "weight=" not in content.split("b1:8080")[1].split("\n")[0]
