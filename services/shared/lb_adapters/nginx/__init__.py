"""NginxAdapter — writes upstream.conf and triggers nginx -s reload via Docker exec."""

from __future__ import annotations

import logging
import os
import socket
import tempfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Optional

from ..base import AdapterState, LoadBalancerAdapter

_log = logging.getLogger("lb_adapter.nginx")

_CONF_HEADER = "upstream backend_pool {\n"
_CONF_FOOTER = "}\n"
# Passive ejection is disabled (max_fails=0). The lb-sidecar actively manages
# pool membership — it removes stopped/excluded backends from this file — so
# NGINX's max_fails is redundant here and actively harmful under load: a backend
# at capacity sheds 503 as graceful backpressure, max_fails would count each 503
# as a "failure", eject every server in turn, and then serve "no live upstreams"
# (502) for the whole overload window. With max_fails=0 a momentarily-shedding
# backend stays in rotation and its 503 is passed back to the client as honest
# backpressure instead of cascading into a total 502 outage.
_SERVER_FMT = "    server {addr} weight={w} max_fails=0;\n"
_SERVER_DOWN_FMT = "    server {addr} down;\n"

# Upper bound on a single DNS lookup during the reload pre-flight. A name that
# does not exist does NOT fail fast through Docker's embedded resolver: libc
# retries with backoff before returning NXDOMAIN (~8 s measured per phantom
# name). The pre-flight runs every lookup in parallel and abandons any that
# overruns this budget, so one unresolvable backend can never stall actuation
# (or the single-threaded control bus that drives it) for more than this.
_DNS_LOOKUP_TIMEOUT_SECONDS = 1.0

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

    def reconcile_excluded(self, live_backends: list[str]) -> bool:
        """Drop exclusions for backends no longer in the live pool.

        Called on a scale event so a stale `down;` (left over from a backend
        the autoscaler has since removed) cannot persist and skew the quorum
        guard against members the live pool no longer contains. Does NOT
        reload on its own — the caller follows with `set_upstream_weights`,
        which renders once. Returns True if anything was pruned.
        """
        live = set(live_backends)
        stale = {b for b in self._excluded if b not in live}
        if not stale:
            return False
        self._excluded -= stale
        return True

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

    def _render_conf(self, skip_active: Optional[set[str]] = None) -> str:
        """Render upstream.conf.

        `skip_active` is the set of currently-unresolvable backend addresses
        (from the DNS pre-flight). They are NOT written as active `server`
        lines — an unresolvable host would make `nginx -s reload` `[emerg]`-
        fail the whole config. An excluded backend that also happens to be
        unresolvable still renders `down;` (a `down` server is not resolved by
        NGINX, so it's safe and preserves the operator's exclusion intent).
        """
        skip_active = skip_active or set()
        lines = [_CONF_HEADER]
        directive = _ALGORITHM_DIRECTIVE.get(self._algorithm)
        if directive:
            lines.append(directive)
        rendered_active = 0
        for addr, weight in sorted(self._weights.items()):
            if addr in self._excluded:
                lines.append(_SERVER_DOWN_FMT.format(addr=addr))
            elif addr in skip_active:
                continue
            else:
                lines.append(_SERVER_FMT.format(addr=addr, w=max(1, weight)))
                rendered_active += 1
        # If no active server was rendered (all excluded, or every active host
        # was unresolvable), add a placeholder so NGINX doesn't error on an
        # empty upstream block.
        if rendered_active == 0:
            lines.append("    # all backends temporarily excluded\n")
        lines.append(_CONF_FOOTER)
        return "".join(lines)

    def _render_and_reload(self) -> None:
        # Quorum safety net (defence-in-depth behind the lb-sidecar's
        # handle_anomaly guard). NGINX serves 502 on an upstream block with
        # no live `server` lines, and that error spike feeds back as more
        # anomaly exclusions — a self-sustaining outage. If every known
        # backend would be excluded we *may* keep the last-known-good upstream
        # and defer: a later include_backend / scale event re-triggers a valid
        # render. (The in-memory `_excluded` intent is still recorded, so the
        # backend renders `down;` again the moment a healthy peer reappears.)
        #
        # The guard only holds if "last-known-good" is actually good: the
        # retained file must still name at least one active server that
        # resolves. When a scale-in removes the only active server, the file
        # on disk freezes pointing at a backend that no longer exists — a
        # known-BAD upstream that 502s every request with no self-heal. In
        # that case we do NOT freeze; we fall through and rewrite to the live
        # render so the dead server stops being served.
        if self._weights and all(b in self._excluded for b in self._weights):
            if self._retained_conf_still_serviceable():
                _log.warning(
                    "refusing nginx reload — all %d backend(s) excluded; "
                    "keeping last-known-good upstream to avoid an empty pool",
                    len(self._weights),
                )
                return
            _log.warning(
                "all %d backend(s) excluded and the retained upstream names "
                "no resolvable active server; rewriting rather than keeping a "
                "known-bad upstream",
                len(self._weights),
            )
        # #155 Risk 3 de-risk — NGINX resolves upstream hostnames at config
        # load time, so a `server` line naming a host that does not currently
        # resolve makes `nginx -s reload` raise `host not found in upstream`
        # and the whole reload fails. Pre-flight every active host; any that
        # does not resolve is OMITTED from the active `server` lines this
        # cycle (rendered out, not written as an active server) so the reload
        # succeeds for the backends that ARE live. The omitted name reappears
        # on the next render once DNS has propagated. We do NOT defer the
        # whole reload: a single unresolvable name must never strand the
        # backends that are serving (the phantom-seed defer bug). The check
        # is bounded + parallel (see `_unresolved_hosts`) so a name that
        # resolves slowly or never can't add seconds to actuation.
        unresolved = self._unresolved_hosts() if self._dns_preflight else set()
        if unresolved:
            _log.warning(
                "omitting %d unresolvable host(s) from the active upstream "
                "this cycle: %s",
                len(unresolved), ", ".join(sorted(unresolved)),
            )
        conf = self._render_conf(skip_active=unresolved)
        self._atomic_write(conf)
        self._reload_nginx()

    def _retained_conf_still_serviceable(self) -> bool:
        """True if the upstream.conf already on disk names ≥1 active server
        that still resolves.

        The quorum guard only keeps the "last-known-good" file when it is
        actually good. After a scale-in that removes the only active server,
        the retained file points at a backend that no longer exists; resolving
        its active servers tells us whether freezing would serve traffic or
        just 502 forever. A missing/unreadable/parse-empty file is treated as
        NOT serviceable so the caller rewrites instead of freezing nothing.
        """
        try:
            text = self._conf_path.read_text()
        except OSError:
            return False
        active_hosts: set[str] = set()
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("server "):
                continue
            parts = stripped.split()
            if len(parts) < 2:
                continue
            if any(p.rstrip(";") == "down" for p in parts):
                continue
            addr = parts[1].rstrip(";")
            host = addr.split(":", 1)[0] if ":" in addr else addr
            if host:
                active_hosts.add(host)
        if not active_hosts:
            return False
        if not self._dns_preflight:
            return True
        return any(self._resolves(h) for h in active_hosts)

    @staticmethod
    def _resolves(host: str, timeout: float = _DNS_LOOKUP_TIMEOUT_SECONDS) -> bool:
        """Resolve a single hostname with a hard wall-clock budget.

        Thin wrapper over `_resolve_many` for the single-host call sites.
        """
        host = host.strip()
        if not host:
            return False
        return host not in NginxAdapter._resolve_many([host], timeout=timeout)

    @staticmethod
    def _resolve_many(
        hosts: list[str],
        timeout: float = _DNS_LOOKUP_TIMEOUT_SECONDS,
    ) -> set[str]:
        """Return the subset of `hosts` that do NOT resolve within `timeout`.

        `socket.gethostbyname` cannot be cancelled and does not fail fast on a
        nonexistent name — Docker's embedded resolver retries with backoff
        (~8 s per phantom name). Every lookup is submitted to a pool and run in
        parallel, and we wait on each future only up to `timeout`, so the whole
        batch is bounded by roughly one timeout window regardless of how many
        names are dead. The pool is shut down with wait=False so a thread still
        stuck in libc resolution can never block teardown either; the orphaned
        daemon worker exits on its own once the resolver finally returns.
        """
        names = [h for h in hosts if h]
        if not names:
            return set()
        pool = ThreadPoolExecutor(max_workers=min(len(names), 8))
        futures = {pool.submit(socket.gethostbyname, h): h for h in names}
        unresolved: set[str] = set()
        try:
            for future, host in futures.items():
                try:
                    future.result(timeout=timeout)
                except (OSError, FuturesTimeoutError):
                    unresolved.add(host)
        finally:
            # Do not block on threads still wedged in a slow/never resolution.
            pool.shutdown(wait=False, cancel_futures=True)
        return unresolved

    def _unresolved_hosts(self) -> set[str]:
        """Return the subset of active `_weights` whose hostname doesn't resolve.

        Excluded backends are skipped — they're rendered as `down;` in
        upstream.conf and don't need a live DNS record. The remaining lookups
        run in parallel via `_resolve_many`, so a batch that includes names
        that never resolve costs at most one timeout window total instead of
        ~8 s per phantom name in series. A name that fails to resolve
        (NXDOMAIN, timeout) is returned so the caller omits it from the active
        server lines this cycle.
        """
        host_to_addr: dict[str, str] = {}
        for addr in self._weights:
            if addr in self._excluded:
                continue
            host = addr.split(":", 1)[0] if ":" in addr else addr
            if host:
                host_to_addr[host] = addr
        if not host_to_addr:
            return set()
        bad_hosts = self._resolve_many(list(host_to_addr))
        return {host_to_addr[h] for h in bad_hosts}

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
        """Populate in-memory state by parsing an existing upstream.conf.

        A `down;` server is re-imported as an exclusion so an operator's
        isolate intent survives a process restart — but ONLY while the host
        still resolves. A stale `down;` for a backend that no longer exists
        (e.g. one the autoscaler decommissioned before this process restarted)
        is dropped rather than inherited: carrying it forward would keep a
        phantom in `_weights`/`_excluded` and let it gate the quorum guard /
        renders against a member the live pool no longer contains.
        """
        try:
            text = self._conf_path.read_text()
        except OSError:
            return
        parsed_down: list[str] = []
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
                parsed_down.append(addr)
            else:
                weight = 1
                for part in parts:
                    if part.startswith("weight="):
                        try:
                            weight = int(part.split("=", 1)[1].rstrip(";"))
                        except ValueError:
                            pass
                self._weights[addr] = weight

        if not parsed_down:
            return
        # Drop stale `down;` entries whose host no longer resolves before they
        # enter the working set (L5). With the pre-flight off (unit tests with
        # synthetic names), inherit them unchanged.
        if self._dns_preflight:
            down_hosts = {
                (addr.split(":", 1)[0] if ":" in addr else addr): addr
                for addr in parsed_down
            }
            stale = self._resolve_many([h for h in down_hosts if h])
            stale_addrs = {down_hosts[h] for h in stale}
        else:
            stale_addrs = set()
        for addr in parsed_down:
            if addr in stale_addrs:
                _log.info(
                    "dropping stale `down;` exclusion for %s — host no longer "
                    "resolves; not inheriting across restart", addr,
                )
                continue
            self._excluded.add(addr)
            if addr not in self._weights:
                self._weights[addr] = 1
