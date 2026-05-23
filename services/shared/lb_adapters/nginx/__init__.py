"""NginxAdapter — writes upstream.conf and triggers nginx -s reload via Docker exec."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from ..base import AdapterState, LoadBalancerAdapter

_CONF_HEADER = "upstream backend_pool {\n"
_CONF_FOOTER = "}\n"
_SERVER_FMT = "    server {addr} weight={w} max_fails=3 fail_timeout=10s;\n"
_SERVER_DOWN_FMT = "    server {addr} down;\n"


class NginxAdapter(LoadBalancerAdapter):
    """Adapter that manages an NGINX upstream block via an include file.

    Args:
        conf_path: Path to the upstream.conf include file the adapter owns.
        nginx_container: Docker container name to exec ``nginx -s reload`` in.
        docker_client: ``docker.DockerClient`` instance (or mock in tests).
        all_backends: Initial set of all known backend addresses (name:port).
    """

    def __init__(
        self,
        conf_path: str | Path,
        nginx_container: str,
        docker_client,
        all_backends: Optional[list[str]] = None,
    ) -> None:
        self._conf_path = Path(conf_path)
        self._nginx_container = nginx_container
        self._docker = docker_client
        self._weights: dict[str, int] = {}
        self._excluded: set[str] = set()

        if all_backends:
            self._weights = {b: 1 for b in all_backends}

        if self._conf_path.exists():
            self._load_state_from_conf()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def set_upstream_weights(self, backend_weights: dict[str, int]) -> None:
        """Replace upstream weights. Excluded backends remain excluded."""
        if backend_weights == self._weights:
            return
        self._weights = dict(backend_weights)
        self._render_and_reload()

    def exclude_backend(self, backend_id: str) -> None:
        """Stop routing to a backend. Idempotent."""
        if backend_id in self._excluded:
            return
        self._excluded.add(backend_id)
        self._render_and_reload()

    def include_backend(self, backend_id: str) -> None:
        """Restore routing to a previously-excluded backend. Idempotent."""
        if backend_id not in self._excluded:
            return
        self._excluded.discard(backend_id)
        self._render_and_reload()

    def current_state(self) -> AdapterState:
        return AdapterState(
            upstream_weights=dict(self._weights),
            excluded_backends=set(self._excluded),
        )

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _render_conf(self) -> str:
        lines = [_CONF_HEADER]
        for addr, weight in sorted(self._weights.items()):
            if addr in self._excluded:
                lines.append(_SERVER_DOWN_FMT.format(addr=addr))
            else:
                lines.append(_SERVER_FMT.format(addr=addr, w=max(1, weight)))
        # If every backend is excluded, add a placeholder so NGINX doesn't
        # error on an empty upstream block.
        if not self._weights or all(b in self._excluded for b in self._weights):
            lines.append("    # all backends temporarily excluded\n")
        lines.append(_CONF_FOOTER)
        return "".join(lines)

    def _render_and_reload(self) -> None:
        conf = self._render_conf()
        self._atomic_write(conf)
        self._reload_nginx()

    def _atomic_write(self, content: str) -> None:
        """Write content to conf_path atomically via tmp+rename."""
        parent = self._conf_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(content)
            os.replace(tmp_path, self._conf_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _reload_nginx(self) -> None:
        container = self._docker.containers.get(self._nginx_container)
        exit_code, output = container.exec_run("nginx -s reload")
        if exit_code != 0:
            raise RuntimeError(
                f"nginx -s reload failed (exit {exit_code}): {output.decode()}"
            )

    def _load_state_from_conf(self) -> None:
        """Populate in-memory state by parsing an existing upstream.conf."""
        try:
            text = self._conf_path.read_text()
        except OSError:
            return
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("server "):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            addr = parts[1].rstrip(";")
            if any(p.rstrip(";") == "down" for p in parts):
                self._excluded.add(addr)
                if addr not in self._weights:
                    self._weights[addr] = 1
            else:
                weight = 1
                for part in parts:
                    if part.startswith("weight="):
                        try:
                            weight = int(part.split("=", 1)[1].rstrip(";"))
                        except ValueError:
                            pass
                self._weights[addr] = weight
