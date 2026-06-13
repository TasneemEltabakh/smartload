"""
services/resource-collector/app.py
──────────────────────────────────
Host-resource shipper — polls the Docker Engine stats API for every
SmartLoad container and emits OTLP/HTTP-JSON gauge data points (CPU %,
memory used / limit / %) to the OTel Collector, on the same pipeline as
the LB OTel shipper (SOT §8.1.1 / §8.3).

Why a dedicated collector (not the access-log shipper or the autoscaler):
  - The access-log shipper (lb-otel-shipper) only sees *request* signals;
    NGINX cannot report a backend's CPU/memory. Those live in the Docker
    Engine's per-container cgroup accounting.
  - The autoscaler already holds a Docker client but its remit is scaling
    decisions; folding metric collection in would entangle the control
    loop's cadence with a telemetry concern. A separate daemon keeps the
    same single-responsibility shape as lb-otel-shipper.

Design (mirrors lb-otel-shipper):
  - Poll `docker.containers.list()` every POLL_INTERVAL_S, filtered to the
    Compose project (COMPOSE_PROJECT, default "smartload"). Re-listed every
    cycle so autoscaler-provisioned backends (#155) appear automatically.
  - `container.stats(stream=False)` returns one sample carrying both
    `cpu_stats` and `precpu_stats`, so a single call yields a CPU delta —
    no second round-trip needed.
  - Emit four canonical flat gauges per container, sharing the telemetry
    `metrics` long format (time, service, instance, metric_name, value):
        cpu_percent          normalised to online CPUs (100 = one full core)
        memory_used_bytes    usage minus reclaimable page cache
        memory_limit_bytes   cgroup memory limit
        memory_percent       used / limit * 100
  - `instance` is keyed to MATCH the request-metric instance column so the
    operator UI can join CPU with rps/latency per backend:
        test-backend containers → "<container-name>:8080"
        every other service     → "<container-name>"
    `service` carries the Compose service name (anomaly-detector, rl-engine,
    test-backend, …) so per-service rollups work too.
  - POST OTLP/HTTP-JSON to the collector with a strict timeout. Any error
    (timeout, conn refused, 5xx, malformed stats) → log + drop, never raise
    into the poll loop. Fire-and-forget at every hop, exactly like the
    access-log shipper.
  - No /health endpoint — process-restart on Docker handles liveness;
    observed indirectly via row arrival in `metrics` (metric_name='cpu_percent').
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import docker
import requests

# ── config ────────────────────────────────────────────────────────────────────

COLLECTOR_URL   = os.environ.get(
    "OTEL_COLLECTOR_URL",
    "http://otel-collector:4318/v1/metrics",
)
INSTANCE_ID     = os.environ.get("INSTANCE_ID", socket.gethostname())
POLL_INTERVAL_S = float(os.environ.get("POLL_INTERVAL_S", "15.0"))
POST_TIMEOUT_S  = float(os.environ.get("POST_TIMEOUT_S", "3.0"))
STATS_WORKERS   = int(os.environ.get("STATS_WORKERS", "8"))
# Only collect containers in this Compose project. SmartLoad pins the
# project name to "smartload" (hardcoded backend hostnames depend on it),
# so this is the safe default; empty string disables the filter (collect
# every container the daemon can see).
COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT", "smartload")
# Don't ship stats for our own container — it would just be noise.
SELF_SERVICE    = os.environ.get("SERVICE_NAME", "resource-collector")
# Compose service label key + the test-backend marker (same constants the
# autoscaler's cluster_client keys off, kept local to avoid importing the
# autoscaler package into this lean daemon).
_COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
_COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
_BACKEND_SERVICE_VALUE = "test-backend"
_BACKEND_PORT          = os.environ.get("BACKEND_PORT", "8080")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [resource-collector] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("resource-collector")


# ── observability counters ────────────────────────────────────────────────────

_lock              = threading.Lock()
_cycles            = 0
_containers_polled = 0
_stats_errors      = 0
_batches_sent      = 0
_batches_dropped   = 0


def _bump(field: str, n: int = 1) -> None:
    global _cycles, _containers_polled, _stats_errors, _batches_sent, _batches_dropped
    with _lock:
        if field == "cycles":
            _cycles += n
        elif field == "containers_polled":
            _containers_polled += n
        elif field == "stats_errors":
            _stats_errors += n
        elif field == "batches_sent":
            _batches_sent += n
        elif field == "batches_dropped":
            _batches_dropped += n


def _stats_snapshot() -> dict:
    with _lock:
        return {
            "cycles":            _cycles,
            "containers_polled": _containers_polled,
            "stats_errors":      _stats_errors,
            "batches_sent":      _batches_sent,
            "batches_dropped":   _batches_dropped,
        }


# ── docker stats → metric values ───────────────────────────────────────────────

def compute_cpu_percent(stats: dict) -> float | None:
    """CPU utilisation as a percent of *all* online cores.

    Uses the standard Docker delta formula (the same maths `docker stats`
    renders): the container's CPU-time delta over the host's CPU-time delta,
    scaled by the number of online cores. 100.0 means one full core; on a
    4-core host a fully-busy container reads 400.0. Returns None when the
    sample lacks the precpu baseline (first read after a container starts).
    """
    try:
        cpu      = stats["cpu_stats"]
        precpu   = stats["precpu_stats"]
        cpu_total    = cpu["cpu_usage"]["total_usage"]
        precpu_total = precpu["cpu_usage"]["total_usage"]
        system_cur   = cpu["system_cpu_usage"]
        system_pre   = precpu.get("system_cpu_usage", 0)
    except (KeyError, TypeError):
        return None

    # Zero baseline = the container's very first stats read (Docker hasn't
    # taken a prior sample yet). The delta would be the whole cumulative
    # usage, ballooning CPU% to a meaningless spike — skip this cycle; the
    # next poll has a real baseline.
    if precpu_total == 0 and system_pre == 0:
        return None

    cpu_delta    = cpu_total - precpu_total
    system_delta = system_cur - system_pre
    if system_delta <= 0 or cpu_delta < 0:
        return None

    online = cpu.get("online_cpus")
    if not online:
        percpu = cpu["cpu_usage"].get("percpu_usage") or []
        online = len(percpu) or 1
    return round((cpu_delta / system_delta) * online * 100.0, 2)


def compute_memory(stats: dict) -> tuple[float | None, float | None, float | None]:
    """Return (used_bytes, limit_bytes, used_percent).

    `used` subtracts reclaimable page cache from the raw usage so the figure
    matches what `docker stats` shows (and what an operator reads as "real"
    memory pressure). cgroup v2 exposes the reclaimable portion as
    `inactive_file`; cgroup v1 as `cache` — we try both. Any missing field
    collapses that component to None rather than raising.
    """
    try:
        mem   = stats["memory_stats"]
        usage = mem["usage"]
        limit = mem["limit"]
    except (KeyError, TypeError):
        return None, None, None

    detail = mem.get("stats", {}) or {}
    reclaimable = detail.get("inactive_file", detail.get("cache", 0)) or 0
    used = max(0.0, float(usage) - float(reclaimable))
    limit_f = float(limit) if limit else None
    pct = round(100.0 * used / limit_f, 2) if limit_f else None
    return round(used, 1), (round(limit_f, 1) if limit_f else None), pct


def datapoints_for(stats: dict, service: str, instance: str, now_ns: int
                   ) -> list[tuple[str, float, int, str, str]]:
    """Build (metric_name, value, time_ns, service, instance) tuples for one
    container's stats sample. Skips any metric whose value couldn't be
    computed (e.g. CPU on the very first sample)."""
    out: list[tuple[str, float, int, str, str]] = []
    cpu = compute_cpu_percent(stats)
    if cpu is not None:
        out.append(("cpu_percent", cpu, now_ns, service, instance))
    used, limit, pct = compute_memory(stats)
    if used is not None:
        out.append(("memory_used_bytes", used, now_ns, service, instance))
    if limit is not None:
        out.append(("memory_limit_bytes", limit, now_ns, service, instance))
    if pct is not None:
        out.append(("memory_percent", pct, now_ns, service, instance))
    return out


def instance_for(service: str, name: str) -> str:
    """Key resource metrics to the SAME `instance` value the request metrics
    use, so the UI can join CPU with rps/latency per backend.

    test-backend replicas → "<name>:<port>" (matches the lb-otel-shipper's
    canonicalised upstream address); every other service → bare container
    name (engines have no per-request instance, so the name is the key)."""
    if service == _BACKEND_SERVICE_VALUE:
        return f"{name}:{_BACKEND_PORT}"
    return name


# ── OTLP/HTTP-JSON envelope (shape per SOT §8.3) ────────────────────────────────

def build_envelope(datapoints: list[tuple[str, float, int, str, str]]) -> dict:
    """Group flat (metric, value, ts, service, instance) tuples into an OTLP
    envelope. One resourceMetrics block per service so the telemetry parser
    reads `service.name` from the resource attributes, and a per-datapoint
    `instance` attribute it prefers over the resource id (telemetry/app.py)."""
    by_service: dict[str, dict[str, list[dict]]] = {}
    for name, value, ts_ns, service, instance in datapoints:
        by_name = by_service.setdefault(service, {})
        by_name.setdefault(name, []).append({
            "timeUnixNano": str(ts_ns),
            "asDouble":     value,
            "attributes":   [
                {"key": "instance", "value": {"stringValue": instance}},
            ],
        })
    resource_metrics = []
    for service, by_name in by_service.items():
        metrics = [
            {"name": name, "gauge": {"dataPoints": dps}}
            for name, dps in by_name.items()
        ]
        resource_metrics.append({
            "resource": {"attributes": [
                {"key": "service.name",        "value": {"stringValue": service}},
                {"key": "service.instance.id", "value": {"stringValue": INSTANCE_ID}},
            ]},
            "scopeMetrics": [{"metrics": metrics}],
        })
    return {"resourceMetrics": resource_metrics}


def post_envelope(envelope: dict) -> None:
    """Fire-and-forget POST. Any error → log + drop, never raise."""
    if not envelope.get("resourceMetrics"):
        return
    try:
        resp = requests.post(COLLECTOR_URL, json=envelope, timeout=POST_TIMEOUT_S)
        if resp.status_code >= 400:
            _bump("batches_dropped")
            log.warning("collector returned %s: %s", resp.status_code, resp.text[:200])
            return
        _bump("batches_sent")
    except requests.RequestException as exc:
        _bump("batches_dropped")
        log.warning("collector POST failed: %s", exc)


# ── poll loop ──────────────────────────────────────────────────────────────────

def _list_targets(client) -> list:
    """Running containers in the Compose project, minus our own container."""
    filters = {"status": "running"}
    if COMPOSE_PROJECT:
        filters["label"] = f"{_COMPOSE_PROJECT_LABEL}={COMPOSE_PROJECT}"
    containers = client.containers.list(filters=filters)
    out = []
    for c in containers:
        service = (c.labels or {}).get(_COMPOSE_SERVICE_LABEL, c.name)
        if service == SELF_SERVICE:
            continue
        out.append(c)
    return out


def _collect_one(container) -> list[tuple[str, float, int, str, str]]:
    """Read one container's stats and turn them into datapoints. Returns []
    (and bumps the error counter) on any failure — one bad container must
    never sink the whole cycle."""
    try:
        stats = container.stats(stream=False)
        service = (container.labels or {}).get(_COMPOSE_SERVICE_LABEL, container.name)
        instance = instance_for(service, container.name)
        return datapoints_for(stats, service, instance, time.time_ns())
    except Exception as exc:                                 # noqa: BLE001
        _bump("stats_errors")
        log.warning("stats(%s) failed: %s", getattr(container, "name", "?"), exc)
        return []


def poll_once(client, pool: ThreadPoolExecutor) -> int:
    """One collection cycle: list targets, fetch stats concurrently, ship.
    Returns the number of containers polled (for tests / heartbeat)."""
    targets = _list_targets(client)
    datapoints: list[tuple[str, float, int, str, str]] = []
    for dps in pool.map(_collect_one, targets):
        datapoints.extend(dps)
    _bump("containers_polled", len(targets))
    _bump("cycles")
    if datapoints:
        post_envelope(build_envelope(datapoints))
    return len(targets)


def run(client, stop_event: threading.Event | None = None) -> None:
    log.info("starting poll loop → %s (project=%s interval=%.1fs)",
             COLLECTOR_URL, COMPOSE_PROJECT or "<all>", POLL_INTERVAL_S)
    pool = ThreadPoolExecutor(max_workers=STATS_WORKERS)
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            start = time.monotonic()
            try:
                poll_once(client, pool)
            except Exception as exc:                          # noqa: BLE001
                # Docker daemon hiccup (socket gone, API error): log + keep
                # the loop alive so we recover when it comes back.
                log.warning("poll cycle failed: %s", exc)
            elapsed = time.monotonic() - start
            time.sleep(max(0.0, POLL_INTERVAL_S - elapsed))
    finally:
        pool.shutdown(wait=False)


# ── entrypoint ──────────────────────────────────────────────────────────────────

def main() -> None:
    def _heartbeat() -> None:
        while True:
            time.sleep(60)
            log.info("stats %s", _stats_snapshot())

    threading.Thread(target=_heartbeat, daemon=True).start()
    client = docker.from_env()
    run(client)


if __name__ == "__main__":
    main()
