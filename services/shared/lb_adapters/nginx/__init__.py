"""NginxAdapter — writes upstream.conf and triggers nginx -s reload via Docker exec."""

from __future__ import annotations

import logging
import os
import socket
import tempfile
from pathlib import Path
from typing import Optional

from ..base import AdapterState, LoadBalancerAdapter

_log = logging.getLogger("lb_adapter.nginx")

_CONF_HEADER = "upstream backend_pool {\n"
_CONF_FOOTER = "}\n"
_SERVER_FMT = "    server {addr} weight={w} max_fails=3 fail_timeout=10s;\n"
_SERVER_DOWN_FMT = "    server {addr} down;\n"

# Algorithms NGINX supports natively via upstream directives.
# "round_robin" is the default and requires no directive.
SUPPORTED_ALGORITHMS = frozenset({"round_robin", "least_conn", "random"})
_ALGORITHM_DIRECTIVE: dict[str, str] = {
    "least_conn": "    least_conn;\n",
    "random":     "    random;\n",
}


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
        *,
        dns_preflight: bool = True,
    ) -> None:
        """
        Args (continued):
            dns_preflight: when True (production default), every backend
                hostname is resolved via ``socket.gethostbyname()`` before
                the upstream.conf is written. If any host fails to resolve
                the reload is deferred and logged — this prevents the
                `host not found in upstream` crash NGINX would otherwise
                raise on a freshly-provisioned backend whose hostname
                hasn't propagated through Docker DNS yet (#155 Risk 3).
                Set False in unit tests that use synthetic backend names
                like ``b1:8080`` which aren't real resolvable hosts.
        """
        self._conf_path = Path(conf_path)
        self._nginx_container = nginx_container
        self._docker = docker_client
        self._dns_preflight = dns_preflight
        self._weights: dict[str, int] = {}
        self._excluded: set[str] = set()
        self._algorithm: str = "round_robin"

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

    def set_algorithm(self, algorithm: str) -> None:
        """Switch the NGINX upstream load-balancing algorithm.

        Resets all server weights to 1 (equal) so the chosen algorithm
        operates without RL-applied bias. The change takes effect on the
        next nginx -s reload triggered by _render_and_reload().

        "round_robin" is the NGINX default and requires no directive.
        "least_conn" adds the least_conn; directive (NGINX uses the backend
        with the fewest active connections).
        "random" adds the random; directive (NGINX picks uniformly at random).
        """
        if algorithm not in SUPPORTED_ALGORITHMS:
            raise ValueError(
                f"Unsupported algorithm: {algorithm!r}. "
                f"Supported: {sorted(SUPPORTED_ALGORITHMS)}"
            )
        if algorithm == self._algorithm:
            return
        self._algorithm = algorithm
        if self._weights:
            self._weights = {b: 1 for b in self._weights}
        self._render_and_reload()

    def current_state(self) -> AdapterState:
        return AdapterState(
            upstream_weights=dict(self._weights),
            excluded_backends=set(self._excluded),
            algorithm=self._algorithm,
        )

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _render_conf(self) -> str:
        lines = [_CONF_HEADER]
        directive = _ALGORITHM_DIRECTIVE.get(self._algorithm)
        if directive:
            lines.append(directive)
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
        # Quorum safety net (defence-in-depth behind the lb-sidecar's
        # handle_anomaly guard). NGINX serves 502 on an upstream block with
        # no live `server` lines, and that error spike feeds back as more
        # anomaly exclusions — a self-sustaining outage. If every known
        # backend would be excluded we keep the last-known-good upstream and
        # defer: a later include_backend / scale event re-triggers a valid
        # render. (The in-memory `_excluded` intent is still recorded, so the
        # backend renders `down;` again the moment a healthy peer reappears.)
        if self._weights and all(b in self._excluded for b in self._weights):
            _log.warning(
                "refusing nginx reload — all %d backend(s) excluded; "
                "keeping last-known-good upstream to avoid an empty pool",
                len(self._weights),
            )
            return
        # #155 Risk 3 de-risk — NGINX resolves upstream hostnames at config
        # load time. If a dynamically-provisioned backend's hostname hasn't
        # propagated through Docker's embedded DNS yet, `nginx -s reload`
        # raises `host not found in upstream`. Pre-flight every host via
        # the host's resolver; if any host is unresolvable we DEFER the
        # reload this cycle and let the next message re-trigger. The new
        # backend will appear on the next discover_all_backends() call
        # after DNS has propagated (≤ 2 s typically).
        if self._dns_preflight:
            unresolved = self._unresolved_hosts()
            if unresolved:
                _log.warning(
                    "deferring nginx reload — %d host(s) not yet resolvable: %s",
                    len(unresolved), ", ".join(sorted(unresolved)),
                )
                return
        conf = self._render_conf()
        self._atomic_write(conf)
        self._reload_nginx()

    def _unresolved_hosts(self) -> set[str]:
        """Return the subset of `_weights` whose hostname doesn't yet resolve.

        Excluded backends are skipped — they're rendered as `down;` in
        upstream.conf and don't need a live DNS record. The resolver call
        is `socket.gethostbyname()` per host; failures (NXDOMAIN, timeout)
        return the unresolvable name into the result set. Returning an
        empty set means every active server will resolve under NGINX too.
        """
        unresolved: set[str] = set()
        for addr in self._weights:
            if addr in self._excluded:
                continue
            host = addr.split(":", 1)[0] if ":" in addr else addr
            if not host:
                continue
            try:
                socket.gethostbyname(host)
            except OSError:
                unresolved.add(addr)
        return unresolved

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
            stripped = line.strip()
            # Detect algorithm directive so state survives a process restart.
            if stripped == "least_conn;":
                self._algorithm = "least_conn"
            elif stripped == "random;":
                self._algorithm = "random"
            if not stripped.startswith("server "):
                continue
            line = stripped
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
