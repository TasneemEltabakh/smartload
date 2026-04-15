"""
SmartLoad Nginx Metrics Exporter

Streams Nginx JSON logs via the Docker SDK (docker logs -f) and exposes
OpenTelemetry metrics via a Prometheus-format /metrics endpoint.

Using docker logs instead of a shared volume file avoids Docker Desktop
Windows filesystem caching issues where file size changes are not visible
to other containers reading the same named volume.
"""

import json
import os
import threading
import time
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler

import docker

# --- OTel SDK ---
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from prometheus_client import generate_latest, REGISTRY, CONTENT_TYPE_LATEST

# =============================================================================
# Configuration
# =============================================================================

SERVICE_NAME        = os.getenv("SERVICE_NAME",        "nginx-lb")
INSTANCE_ID         = os.getenv("INSTANCE_ID",         "nginx-001")
NODE_ID             = os.getenv("NODE_ID",             "a0000000-0000-0000-0000-000000000001")
METRICS_PORT        = int(os.getenv("METRICS_PORT",    "9113"))
SOURCE              = os.getenv("SOURCE",              "real")
ENVIRONMENT         = os.getenv("ENVIRONMENT",         "development")
NGINX_CONTAINER_NAME = os.getenv("NGINX_CONTAINER_NAME", "infrastructure-nginx-1")

WINDOW_SECONDS = 60

# =============================================================================
# OTel SDK setup
# =============================================================================

resource = Resource.create({
    "service.name":           SERVICE_NAME,
    "service.instance.id":    INSTANCE_ID,
    "node.id":                NODE_ID,
    "deployment.environment": ENVIRONMENT,
})

prometheus_reader = PrometheusMetricReader()
provider = MeterProvider(resource=resource, metric_readers=[prometheus_reader])
metrics.set_meter_provider(provider)

meter = metrics.get_meter("smartload.nginx.exporter", version="1.0.0")

# =============================================================================
# Sliding window state — defined before instruments that reference the callback
# =============================================================================

_lock = threading.Lock()
_request_window: deque = deque()
_error_window:   deque = deque()


def _error_rate_callback(options):
    now    = time.time()
    cutoff = now - WINDOW_SECONDS
    with _lock:
        while _request_window and _request_window[0] < cutoff:
            _request_window.popleft()
        while _error_window and _error_window[0] < cutoff:
            _error_window.popleft()
        rate = len(_error_window) / len(_request_window) if _request_window else 0.0

    yield metrics.Observation(
        rate,
        attributes={
            "service_name": SERVICE_NAME,
            "instance_id":  INSTANCE_ID,
            "node_id":      NODE_ID,
            "source":       SOURCE,
            "environment":  ENVIRONMENT,
        },
    )


# =============================================================================
# Instrument definitions
# unit="" prevents PrometheusMetricReader from appending a suffix to the name
# =============================================================================

request_counter = meter.create_counter(
    name="smartload_request_count_total",
    description="Total number of requests handled by the load balancer",
    unit="",
)

request_latency = meter.create_histogram(
    name="smartload_request_latency_ms",
    description="Request latency in milliseconds",
    unit="",
)

error_counter = meter.create_counter(
    name="smartload_error_count_total",
    description="Total number of error responses (4xx and 5xx)",
    unit="",
)

backend_request_counter = meter.create_counter(
    name="smartload_routing_backend_requests_total",
    description="Requests routed to each backend",
    unit="",
)

backend_latency_hist = meter.create_histogram(
    name="smartload_backend_latency_ms",
    description="Backend upstream latency in milliseconds",
    unit="",
)

error_rate_gauge = meter.create_observable_gauge(
    name="smartload_error_rate",
    callbacks=[_error_rate_callback],
    description="Error rate over the last 60-second sliding window (0.0-1.0)",
    unit="",
)

# =============================================================================
# Common label attributes
# =============================================================================

BASE_ATTRS = {
    "service_name": SERVICE_NAME,
    "instance_id":  INSTANCE_ID,
    "node_id":      NODE_ID,
    "source":       SOURCE,
    "environment":  ENVIRONMENT,
}

# =============================================================================
# Log parsing
# =============================================================================

def parse_nginx_log_line(line: str) -> dict | None:
    try:
        brace = line.find("{")
        if brace == -1:
            return None
        data = json.loads(line[brace:])

        if "request" not in data:
            return None

        parts  = data.get("request", "GET / HTTP/1.1").split()
        method = parts[0] if parts else "UNKNOWN"

        path       = data.get("request_path", "/")
        status     = int(data.get("status", 0))
        backend    = data.get("backend", "unknown") or "unknown"
        latency_ms = float(data.get("latency", 0)) * 1000

        raw_upstream = str(data.get("upstream_latency", "-")).strip()
        upstream_ms  = 0.0
        if raw_upstream and raw_upstream != "-":
            tokens = [t.strip() for t in raw_upstream.replace(":", " ").split()
                      if t.strip() != ":"]
            for tok in reversed(tokens):
                try:
                    upstream_ms = float(tok) * 1000
                    break
                except ValueError:
                    continue

        return {
            "method":      method,
            "path":        path,
            "status":      status,
            "backend":     backend,
            "latency_ms":  latency_ms,
            "upstream_ms": upstream_ms,
        }

    except Exception:
        return None

# =============================================================================
# Metric recording
# =============================================================================

def record_metrics(p: dict) -> None:
    if not p:
        return

    request_counter.add(1, BASE_ATTRS)
    request_latency.record(
        p["latency_ms"],
        {**BASE_ATTRS, "path": p["path"], "method": p["method"]},
    )
    backend_request_counter.add(1, {**BASE_ATTRS, "backend": p["backend"]})

    if p["upstream_ms"] > 0:
        backend_latency_hist.record(
            p["upstream_ms"],
            {**BASE_ATTRS, "backend": p["backend"]},
        )

    status = p["status"]
    if status >= 500:
        error_counter.add(1, {**BASE_ATTRS, "status_class": "5xx"})
    elif status >= 400:
        error_counter.add(1, {**BASE_ATTRS, "status_class": "4xx"})

    now = time.time()
    with _lock:
        _request_window.append(now)
        if status >= 400:
            _error_window.append(now)

    print(f"[METRIC] {p['method']} {p['path']} -> {status} ({p['latency_ms']:.1f}ms)")

# =============================================================================
# Docker log streamer (background thread)
# =============================================================================

def stream_docker_logs() -> None:
    client = docker.from_env()

    while True:
        try:
            container = client.containers.get(NGINX_CONTAINER_NAME)
            print(f"[INFO] Connected to container: {NGINX_CONTAINER_NAME}")

            # stream=True, follow=True gives us a live byte stream
            # since=0 means from now only (skip historical logs)
            log_stream = container.logs(
                stream=True,
                follow=True,
                since=int(time.time()),
            )

            for chunk in log_stream:
                # Each chunk may contain multiple newline-separated log lines
                text = chunk.decode("utf-8", errors="replace")
                for line in text.splitlines():
                    line = line.strip()
                    if line:
                        parsed = parse_nginx_log_line(line)
                        record_metrics(parsed)

        except docker.errors.NotFound:
            print(f"[WARN] Container {NGINX_CONTAINER_NAME} not found, retrying...")
            time.sleep(3)
        except Exception as e:
            print(f"[WARN] Log stream error: {e}, reconnecting in 3s...")
            time.sleep(3)

# =============================================================================
# HTTP server
# =============================================================================

class MetricsHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == "/metrics":
            output = generate_latest(REGISTRY)
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(output)))
            self.end_headers()
            self.wfile.write(output)

        elif self.path == "/health":
            body = json.dumps({
                "status":      "healthy",
                "service":     SERVICE_NAME,
                "instance_id": INSTANCE_ID,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()

# =============================================================================
# Entry point
# =============================================================================

def main():
    print(f"[INFO] SmartLoad Nginx Metrics Exporter starting")
    print(f"[INFO] Service: {SERVICE_NAME} | Instance: {INSTANCE_ID} | Node: {NODE_ID}")
    print(f"[INFO] Nginx container: {NGINX_CONTAINER_NAME}")
    print(f"[INFO] Metrics port: {METRICS_PORT}")

    t = threading.Thread(target=stream_docker_logs, daemon=True)
    t.start()

    server = HTTPServer(("0.0.0.0", METRICS_PORT), MetricsHandler)
    print(f"[INFO] Metrics server listening on :{METRICS_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()