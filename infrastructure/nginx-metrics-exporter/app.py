"""
SmartLoad Nginx Metrics Exporter
Reads Nginx logs via Docker API and exposes Prometheus metrics.
Follows telemetry-v1 schema: smartload.<domain>.<metric>
"""

import json
import os
import subprocess
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    generate_latest,
    REGISTRY,
    CONTENT_TYPE_LATEST
)

# =============================================================================
# Configuration
# =============================================================================

SERVICE_NAME = os.getenv("SERVICE_NAME", "nginx-lb")
INSTANCE_ID = os.getenv("INSTANCE_ID", "nginx-001")
NODE_ID = os.getenv("NODE_ID", "a0000000-0000-0000-0000-000000000001")
NGINX_CONTAINER_NAME = os.getenv("NGINX_CONTAINER_NAME", "infrastructure-nginx-1")
METRICS_PORT = int(os.getenv("METRICS_PORT", "9113"))
SOURCE = os.getenv("SOURCE", "real")
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

# =============================================================================
# Prometheus Metrics (following smartload telemetry-v1 schema)
# =============================================================================

LABELS = ["service_name", "instance_id", "node_id", "source", "environment"]
BACKEND_LABELS = LABELS + ["backend"]
PATH_LABELS = LABELS + ["path", "method"]

REQUEST_COUNT = Counter(
    "smartload_request_count_total",
    "Total number of requests handled by the load balancer",
    LABELS
)

REQUEST_LATENCY = Histogram(
    "smartload_request_latency_ms",
    "Request latency in milliseconds",
    PATH_LABELS,
    buckets=[5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]
)

ERROR_COUNT = Counter(
    "smartload_error_count_total",
    "Total number of error responses (4xx and 5xx)",
    LABELS + ["status_class"]
)

BACKEND_REQUEST_COUNT = Counter(
    "smartload_routing_backend_requests_total",
    "Requests routed to each backend",
    BACKEND_LABELS
)

BACKEND_LATENCY = Histogram(
    "smartload_backend_latency_ms",
    "Upstream backend latency in milliseconds",
    BACKEND_LABELS,
    buckets=[5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]
)

ERROR_RATE = Gauge(
    "smartload_error_rate",
    "Current error rate (ratio 0.0-1.0) over sliding window",
    LABELS
)

# Sliding window for error rate
_request_window = []
_error_window = []
_window_lock = threading.Lock()
WINDOW_SECONDS = 60

# =============================================================================
# Log Parsing
# =============================================================================

def parse_nginx_log_line(line):
    """
    Parse a single Nginx JSON log line.
    Expected format:
    {
        "timestamp": "...",
        "service": "nginx",
        "client_ip": "...",
        "request": "GET / HTTP/1.1",
        "request_path": "/",
        "status": 200,
        "backend": "172.18.0.2:8080",
        "latency": 0.005,
        "upstream_latency": "0.004"
    }
    """
    try:
        # Handle Docker log format: timestamp + space + JSON
        if line and line[0].isdigit():
            # Docker adds timestamp prefix, find the JSON part
            json_start = line.find('{')
            if json_start != -1:
                line = line[json_start:]
        
        data = json.loads(line.strip())
        
        # Skip non-request logs (nginx startup messages etc)
        if "request" not in data or "status" not in data:
            return None
        
        request_str = data.get("request", "GET / HTTP/1.1")
        method = request_str.split()[0] if request_str else "GET"
        
        path = data.get("request_path", "/")
        path = path.split("?")[0]
        if len(path) > 50:
            path = path[:50] + "..."
        
        latency_sec = float(data.get("latency", 0))
        latency_ms = latency_sec * 1000
        
        upstream_latency_str = data.get("upstream_latency", "0")
        if upstream_latency_str == "-" or upstream_latency_str == "":
            upstream_latency_ms = 0
        else:
            upstream_latency_ms = float(upstream_latency_str) * 1000
        
        return {
            "timestamp": data.get("timestamp"),
            "method": method,
            "path": path,
            "status": int(data.get("status", 0)),
            "backend": data.get("backend", "unknown"),
            "latency_ms": latency_ms,
            "upstream_latency_ms": upstream_latency_ms,
            "client_ip": data.get("client_ip", "")
        }
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        return None


def record_metrics(parsed):
    """Record metrics from a parsed log entry."""
    if not parsed:
        return
    
    labels = {
        "service_name": SERVICE_NAME,
        "instance_id": INSTANCE_ID,
        "node_id": NODE_ID,
        "source": SOURCE,
        "environment": ENVIRONMENT
    }
    
    REQUEST_COUNT.labels(**labels).inc()
    
    path_labels = {**labels, "path": parsed["path"], "method": parsed["method"]}
    REQUEST_LATENCY.labels(**path_labels).observe(parsed["latency_ms"])
    
    backend_labels = {**labels, "backend": parsed["backend"]}
    BACKEND_REQUEST_COUNT.labels(**backend_labels).inc()
    
    if parsed["upstream_latency_ms"] > 0:
        BACKEND_LATENCY.labels(**backend_labels).observe(parsed["upstream_latency_ms"])
    
    status = parsed["status"]
    is_error = status >= 400
    
    if status >= 500:
        ERROR_COUNT.labels(**labels, status_class="5xx").inc()
    elif status >= 400:
        ERROR_COUNT.labels(**labels, status_class="4xx").inc()
    
    now = time.time()
    with _window_lock:
        _request_window.append(now)
        if is_error:
            _error_window.append(now)
        
        cutoff = now - WINDOW_SECONDS
        while _request_window and _request_window[0] < cutoff:
            _request_window.pop(0)
        while _error_window and _error_window[0] < cutoff:
            _error_window.pop(0)
        
        total = len(_request_window)
        errors = len(_error_window)
        rate = errors / total if total > 0 else 0.0
        ERROR_RATE.labels(**labels).set(rate)
    
    print(f"[METRIC] {parsed['method']} {parsed['path']} -> {parsed['status']} ({parsed['latency_ms']:.1f}ms)")


# =============================================================================
# Docker Log Reader
# =============================================================================

def read_docker_logs():
    """
    Read nginx container logs using docker logs command.
    This works reliably across all platforms.
    """
    print(f"[INFO] Starting Docker log reader for container: {NGINX_CONTAINER_NAME}")
    print(f"[INFO] Waiting for container to be available...")
    
    # Wait for container to be running
    while True:
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", NGINX_CONTAINER_NAME],
                capture_output=True,
                text=True
            )
            if result.returncode == 0 and "true" in result.stdout.lower():
                print(f"[INFO] Container {NGINX_CONTAINER_NAME} is running")
                break
        except Exception as e:
            print(f"[WARN] Waiting for container: {e}")
        time.sleep(2)
    
    # Start tailing logs
    print(f"[INFO] Starting log tail...")
    
    process = subprocess.Popen(
        ["docker", "logs", "-f", "--since", "1s", NGINX_CONTAINER_NAME],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    
    for line in process.stdout:
        line = line.strip()
        if line and '{' in line:
            parsed = parse_nginx_log_line(line)
            if parsed:
                record_metrics(parsed)


# =============================================================================
# Prometheus HTTP Handler
# =============================================================================

class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(generate_latest(REGISTRY))
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "healthy",
                "service": SERVICE_NAME,
                "instance_id": INSTANCE_ID
            }).encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass


# =============================================================================
# Main
# =============================================================================

def main():
    print(f"[INFO] SmartLoad Nginx Metrics Exporter")
    print(f"[INFO] Service: {SERVICE_NAME}, Instance: {INSTANCE_ID}")
    print(f"[INFO] Target container: {NGINX_CONTAINER_NAME}")
    print(f"[INFO] Metrics endpoint: http://0.0.0.0:{METRICS_PORT}/metrics")
    
    # Start Docker log reader in background thread
    reader = threading.Thread(target=read_docker_logs, daemon=True)
    reader.start()
    
    # Start metrics HTTP server
    server = HTTPServer(("0.0.0.0", METRICS_PORT), MetricsHandler)
    print(f"[INFO] Prometheus metrics server started on port {METRICS_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()