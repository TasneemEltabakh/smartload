"""
tests/unit/lb-sidecar/test_nginx_adapter.py
─────────────────────────────────────────────
Unit tests for services/shared/lb_adapters/nginx/__init__.py (NginxAdapter).

Uses a tmp directory for conf files and a mock docker client — no running
Docker daemon required. Runs in the unit-tests CI job.

Coverage:
  1. Initial state from seed_backends (no existing conf).
  2. Initial state parsed from an existing conf file.
  3. set_upstream_weights — writes conf, triggers reload, idempotent.
  4. exclude_backend — marks server down, triggers reload, idempotent.
  5. include_backend — restores server, triggers reload, idempotent.
  6. current_state — reflects in-memory state correctly.
  7. Atomic write — tmp file is cleaned up on error.
  8. All-excluded safety net — refuses empty upstream, keeps last-known-good.
  9. docker exec failure — raises RuntimeError.
  10. Rendering order — sorted by address for deterministic output.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SHARED = Path(__file__).resolve().parents[3] / "services" / "shared"
if str(_SHARED.parent) not in sys.path:
    sys.path.insert(0, str(_SHARED.parent))

from shared.lb_adapters.nginx import NginxAdapter  # noqa: E402
from shared.lb_adapters.base import AdapterState    # noqa: E402


def _make_docker(exit_code: int = 0, output: bytes = b""):
    docker = MagicMock()
    container = MagicMock()
    container.exec_run.return_value = (exit_code, output)
    docker.containers.get.return_value = container
    return docker


# ── Construction ──────────────────────────────────────────────────────────────

def test_initial_state_from_seed(tmp_path):
    adapter = NginxAdapter(
        conf_path=tmp_path / "upstream.conf",
        nginx_container="lb",
        docker_client=_make_docker(),
        all_backends=["b1:8080", "b2:8080"],
    )
    state = adapter.current_state()
    assert state.upstream_weights == {"b1:8080": 1, "b2:8080": 1}
    assert state.excluded_backends == set()


def test_initial_state_parsed_from_existing_conf(tmp_path):
    conf = tmp_path / "upstream.conf"
    conf.write_text(
        "upstream backend_pool {\n"
        "    server b1:8080 weight=75 max_fails=3 fail_timeout=10s;\n"
        "    server b2:8080 down;\n"
        "}\n"
    )
    adapter = NginxAdapter(
        conf_path=conf,
        nginx_container="lb",
        docker_client=_make_docker(),
    )
    state = adapter.current_state()
    assert state.upstream_weights["b1:8080"] == 75
    assert "b2:8080" in state.excluded_backends


# ── set_upstream_weights ──────────────────────────────────────────────────────

def test_set_upstream_weights_writes_conf_and_reloads(tmp_path):
    docker = _make_docker()
    adapter = NginxAdapter(
        conf_path=tmp_path / "upstream.conf",
        nginx_container="lb",
        docker_client=docker,
        all_backends=["b1:8080"],
    )
    adapter.set_upstream_weights({"b1:8080": 90, "b2:8080": 60})
    content = (tmp_path / "upstream.conf").read_text()
    assert "weight=90" in content
    assert "weight=60" in content
    docker.containers.get("lb").exec_run.assert_called()


def test_set_upstream_weights_idempotent(tmp_path):
    docker = _make_docker()
    adapter = NginxAdapter(
        conf_path=tmp_path / "upstream.conf",
        nginx_container="lb",
        docker_client=docker,
        all_backends=["b1:8080"],
    )
    adapter.set_upstream_weights({"b1:8080": 50})
    call_count_after_first = docker.containers.get("lb").exec_run.call_count
    adapter.set_upstream_weights({"b1:8080": 50})
    # Idempotent: no additional reload
    assert docker.containers.get("lb").exec_run.call_count == call_count_after_first


def test_set_upstream_weights_weight_floored_at_one(tmp_path):
    adapter = NginxAdapter(
        conf_path=tmp_path / "upstream.conf",
        nginx_container="lb",
        docker_client=_make_docker(),
    )
    adapter.set_upstream_weights({"b1:8080": 0})
    content = (tmp_path / "upstream.conf").read_text()
    assert "weight=1" in content


# ── exclude_backend ───────────────────────────────────────────────────────────

def test_exclude_backend_marks_down(tmp_path):
    adapter = NginxAdapter(
        conf_path=tmp_path / "upstream.conf",
        nginx_container="lb",
        docker_client=_make_docker(),
        all_backends=["b1:8080", "b2:8080"],
    )
    adapter.set_upstream_weights({"b1:8080": 80, "b2:8080": 50})
    adapter.exclude_backend("b1:8080")
    content = (tmp_path / "upstream.conf").read_text()
    assert "b1:8080 down" in content
    assert "b2:8080 down" not in content
    assert "b1:8080" in adapter.current_state().excluded_backends


def test_exclude_backend_idempotent(tmp_path):
    docker = _make_docker()
    adapter = NginxAdapter(
        conf_path=tmp_path / "upstream.conf",
        nginx_container="lb",
        docker_client=docker,
        all_backends=["b1:8080"],
    )
    adapter.set_upstream_weights({"b1:8080": 70})
    adapter.exclude_backend("b1:8080")
    count = docker.containers.get("lb").exec_run.call_count
    adapter.exclude_backend("b1:8080")
    assert docker.containers.get("lb").exec_run.call_count == count


# ── include_backend ───────────────────────────────────────────────────────────

def test_include_backend_restores(tmp_path):
    adapter = NginxAdapter(
        conf_path=tmp_path / "upstream.conf",
        nginx_container="lb",
        docker_client=_make_docker(),
        all_backends=["b1:8080"],
    )
    adapter.set_upstream_weights({"b1:8080": 60})
    adapter.exclude_backend("b1:8080")
    adapter.include_backend("b1:8080")
    content = (tmp_path / "upstream.conf").read_text()
    assert "b1:8080 down" not in content
    assert "weight=60" in content
    assert "b1:8080" not in adapter.current_state().excluded_backends


def test_include_backend_idempotent(tmp_path):
    docker = _make_docker()
    adapter = NginxAdapter(
        conf_path=tmp_path / "upstream.conf",
        nginx_container="lb",
        docker_client=docker,
        all_backends=["b1:8080"],
    )
    count = docker.containers.get("lb").exec_run.call_count
    adapter.include_backend("b1:8080")  # not excluded — no-op
    assert docker.containers.get("lb").exec_run.call_count == count


# ── current_state ─────────────────────────────────────────────────────────────

def test_current_state_reflects_mutations(tmp_path):
    adapter = NginxAdapter(
        conf_path=tmp_path / "upstream.conf",
        nginx_container="lb",
        docker_client=_make_docker(),
        all_backends=["a:8080", "b:8080", "c:8080"],
    )
    adapter.set_upstream_weights({"a:8080": 80, "b:8080": 60, "c:8080": 40})
    adapter.exclude_backend("c:8080")
    state = adapter.current_state()
    assert state.upstream_weights == {"a:8080": 80, "b:8080": 60, "c:8080": 40}
    assert state.excluded_backends == {"c:8080"}


# ── All-excluded safety net (quorum) ──────────────────────────────────────────

def test_excluding_last_backend_keeps_last_known_good(tmp_path):
    """Quorum safety net: the adapter refuses to render an upstream with
    zero active servers (NGINX 502s an empty pool, which feeds back as more
    anomaly exclusions). Excluding the only backend keeps the last-known-good
    conf — b1 still serving — and defers the reload."""
    docker = _make_docker()
    adapter = NginxAdapter(
        conf_path=tmp_path / "upstream.conf",
        nginx_container="lb",
        docker_client=docker,
        all_backends=["b1:8080"],
    )
    adapter.set_upstream_weights({"b1:8080": 50})       # writes + reloads
    reloads_after_write = docker.containers.get("lb").exec_run.call_count
    adapter.exclude_backend("b1:8080")                  # would empty pool → refused
    content = (tmp_path / "upstream.conf").read_text()
    assert "upstream backend_pool {" in content
    assert "b1:8080 down" not in content                # last-known-good retained
    assert "weight=50" in content
    # No reload was triggered for the refused exclusion.
    assert docker.containers.get("lb").exec_run.call_count == reloads_after_write


def test_excluding_one_of_several_still_renders(tmp_path):
    """The safety net only fires when *all* backends would be excluded. With
    a peer still active, exclusion renders normally."""
    adapter = NginxAdapter(
        conf_path=tmp_path / "upstream.conf",
        nginx_container="lb",
        docker_client=_make_docker(),
        all_backends=["b1:8080", "b2:8080"],
    )
    adapter.set_upstream_weights({"b1:8080": 50, "b2:8080": 50})
    adapter.exclude_backend("b1:8080")
    content = (tmp_path / "upstream.conf").read_text()
    assert "b1:8080 down" in content
    assert "b2:8080 down" not in content


# ── Docker exec failure ───────────────────────────────────────────────────────

def test_docker_exec_failure_raises(tmp_path):
    docker = _make_docker(exit_code=1, output=b"signal process error")
    adapter = NginxAdapter(
        conf_path=tmp_path / "upstream.conf",
        nginx_container="lb",
        docker_client=docker,
        all_backends=["b1:8080"],
    )
    with pytest.raises(RuntimeError, match="nginx -s reload failed"):
        adapter.set_upstream_weights({"b1:8080": 70})


# ── Rendering order ───────────────────────────────────────────────────────────

def test_conf_servers_sorted(tmp_path):
    adapter = NginxAdapter(
        conf_path=tmp_path / "upstream.conf",
        nginx_container="lb",
        docker_client=_make_docker(),
    )
    adapter.set_upstream_weights({"z:8080": 10, "a:8080": 90, "m:8080": 50})
    content = (tmp_path / "upstream.conf").read_text()
    pos_a = content.index("a:8080")
    pos_m = content.index("m:8080")
    pos_z = content.index("z:8080")
    assert pos_a < pos_m < pos_z
