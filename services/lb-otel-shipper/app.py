"""
services/lb-otel-shipper/app.py
───────────────────────────────
T1.2 sidecar — tails NGINX's JSON access log on the shared `nginx-logs`
volume and emits one OTLP/HTTP-JSON gauge data point per request, per
canonical flat metric name, to the OTel Collector.

Per SOT §8.1.1 (LB OTel Shipper):
  - Approach B (log-based shipper) is canonical, not the F5 `nginx-otel`
    module. Reason: the AI engines query AVG / MAX / STDDEV over rolling
    windows from `metrics`; that requires *per-request* rows, not
    histograms aggregated upstream of the DB.
  - Tail `/var/log/nginx/access.log` (seek from end on start; do not
    re-emit historical lines). Restart loses position by design.
  - Tolerate malformed log lines: log + skip, never crash.
  - For each request emit three OTLP gauge data points sharing resource
    attributes:
        request_count        = 1
        request_latency_ms   = latency * 1000  (NGINX $request_time is seconds)
        error_rate           = 1 if status >= 500 else 0
  - Resource attrs: service.name=load-balancer, service.instance.id=<hostname>.
  - POST OTLP/HTTP-JSON to http://otel-collector:4318/v1/metrics with a
    strict timeout. On any error (timeout, conn refused, 5xx) → log + drop.
    Never raise into the tailing loop. Fire-and-forget at every hop.
  - No /health endpoint at T1.2 — process-restart on Docker handles
    liveness; observed indirectly via row arrival in `metrics`.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
from collections import deque

import requests

# ── config ────────────────────────────────────────────────────────────────────

LOG_PATH        = os.environ.get("NGINX_LOG_PATH", "/nginx-logs/access.log")
COLLECTOR_URL   = os.environ.get(
    "OTEL_COLLECTOR_URL",
    "http://otel-collector:4318/v1/metrics",
)
SERVICE_NAME    = os.environ.get("SERVICE_NAME", "load-balancer")
INSTANCE_ID     = os.environ.get("INSTANCE_ID", socket.gethostname())
POST_TIMEOUT_S  = float(os.environ.get("POST_TIMEOUT_S", "2.0"))
FLUSH_INTERVAL  = float(os.environ.get("FLUSH_INTERVAL_S", "1.0"))
FLUSH_BATCH_MAX = int(os.environ.get("FLUSH_BATCH_MAX", "500"))
REOPEN_BACKOFF  = float(os.environ.get("REOPEN_BACKOFF_S", "1.0"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [lb-otel-shipper] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("lb-otel-shipper")


# ── observability counters ────────────────────────────────────────────────────

_lock              = threading.Lock()
_lines_parsed      = 0
_lines_skipped     = 0
_batches_sent      = 0
_batches_dropped   = 0


def _bump(field: str, n: int = 1) -> None:
    global _lines_parsed, _lines_skipped, _batches_sent, _batches_dropped
    with _lock:
        if field == "lines_parsed":
            _lines_parsed += n
        elif field == "lines_skipped":
            _lines_skipped += n
        elif field == "batches_sent":
            _batches_sent += n
        elif field == "batches_dropped":
            _batches_dropped += n


def _stats_snapshot() -> dict:
    with _lock:
        return {
            "lines_parsed":    _lines_parsed,
            "lines_skipped":   _lines_skipped,
            "batches_sent":    _batches_sent,
            "batches_dropped": _batches_dropped,
        }


# ── log line → metric data points ─────────────────────────────────────────────

def line_to_datapoints(line: str, now_ns: int) -> list[tuple[str, float, int]]:
    """Parse one JSON access-log line into (metric_name, value, time_ns) tuples.

    Returns an empty list for malformed input (the caller bumps lines_skipped).
    The three canonical metric names are hardcoded — they are the names that
    `services/shared/queries.py:ANOMALY_QUERY` and RL_STATE_QUERY filter on.
    """
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return []

    latency_s = record.get("latency")
    status    = record.get("status")
    if latency_s is None or status is None:
        return []

    try:
        latency_ms = float(latency_s) * 1000.0
        status_int = int(status)
    except (TypeError, ValueError):
        return []

    error_rate = 1.0 if status_int >= 500 else 0.0
    return [
        ("request_count",      1.0,        now_ns),
        ("request_latency_ms", latency_ms, now_ns),
        ("error_rate",         error_rate, now_ns),
    ]


# ── OTLP/HTTP-JSON envelope ───────────────────────────────────────────────────
# Shape per SOT §8.3 (Telemetry parser): resourceMetrics[].scopeMetrics[].
# metrics[].gauge.dataPoints[] with asDouble + timeUnixNano. Histograms /
# summaries are out of the SmartLoad metric set (telemetry drops them).

def build_envelope(datapoints: list[tuple[str, float, int]]) -> dict:
    by_name: dict[str, list[dict]] = {}
    for name, value, ts_ns in datapoints:
        by_name.setdefault(name, []).append({
            "timeUnixNano": str(ts_ns),
            "asDouble":     value,
        })
    metrics = [
        {"name": name, "gauge": {"dataPoints": dps}}
        for name, dps in by_name.items()
    ]
    return {
        "resourceMetrics": [{
            "resource": {"attributes": [
                {"key": "service.name",        "value": {"stringValue": SERVICE_NAME}},
                {"key": "service.instance.id", "value": {"stringValue": INSTANCE_ID}},
            ]},
            "scopeMetrics": [{"metrics": metrics}],
        }],
    }


def post_envelope(envelope: dict) -> None:
    """Fire-and-forget POST. Any error → log + drop, never raise."""
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


# ── tail loop ─────────────────────────────────────────────────────────────────

def _open_at_end(path: str):
    """Open the log file and seek to end; return None if the file is missing."""
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    fh.seek(0, os.SEEK_END)
    return fh


def tail_and_ship(path: str, stop_event: threading.Event | None = None) -> None:
    """Tail `path`, emit OTLP. Runs until stop_event is set or the process dies.

    Re-opens the file if a read returns nothing for `REOPEN_BACKOFF` seconds —
    handles log rotation cheaply without inotify.
    """
    log.info("starting tail loop on %s → %s (service=%s instance=%s)",
             path, COLLECTOR_URL, SERVICE_NAME, INSTANCE_ID)

    fh = None
    while fh is None:
        if stop_event is not None and stop_event.is_set():
            return
        fh = _open_at_end(path)
        if fh is None:
            log.info("waiting for %s to appear", path)
            time.sleep(REOPEN_BACKOFF)

    buffer: deque[tuple[str, float, int]] = deque()
    last_flush = time.monotonic()

    while True:
        if stop_event is not None and stop_event.is_set():
            break

        line = fh.readline()
        if line:
            now_ns = time.time_ns()
            dps    = line_to_datapoints(line, now_ns)
            if not dps:
                _bump("lines_skipped")
            else:
                _bump("lines_parsed")
                buffer.extend(dps)

        now = time.monotonic()
        should_flush = (
            buffer and (
                len(buffer) >= FLUSH_BATCH_MAX
                or (now - last_flush) >= FLUSH_INTERVAL
            )
        )
        if should_flush:
            batch = list(buffer)
            buffer.clear()
            last_flush = now
            post_envelope(build_envelope(batch))

        if not line:
            time.sleep(0.05)


# ── entrypoint ────────────────────────────────────────────────────────────────

def main() -> None:
    def _heartbeat() -> None:
        while True:
            time.sleep(30)
            log.info("stats %s", _stats_snapshot())

    threading.Thread(target=_heartbeat, daemon=True).start()
    tail_and_ship(LOG_PATH)


if __name__ == "__main__":
    main()
