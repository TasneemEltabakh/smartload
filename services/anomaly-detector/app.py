import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from config import load_config
from detector import AnomalyDetector
from publisher import RedisStatusPublisher
from repository import JsonTelemetryRepository


logging.basicConfig(level=logging.INFO, format="%(message)s")


class AnomalyDetectionService:
    def __init__(self, config):
        self.config = config
        self.repository = JsonTelemetryRepository(config.telemetry_file)
        self.detector = AnomalyDetector(config)
        self.publisher = RedisStatusPublisher(config)
        self._lock = threading.Lock()
        self._latest_report = {
            "service": config.service_name,
            "last_run_at": None,
            "results": []
        }

    def analyze_once(self):
        records = self.repository.fetch_all()
        results = self.detector.evaluate(records)
        for result in results:
            self.publisher.publish(result)
            if result["state"] != "healthy":
                logging.info(json.dumps({
                    "event": "anomaly_detected",
                    "timestamp": result["timestamp"],
                    "node_id": result["node_id"],
                    "service_name": result["service_name"],
                    "state": result["state"],
                    "reason": result["reason"],
                    "metrics": result["trigger_metrics"]
                }))

        report = {
            "service": self.config.service_name,
            "last_run_at": datetime.now(timezone.utc).isoformat(),
            "results": results
        }
        with self._lock:
            self._latest_report = report
        return report

    def get_report(self):
        with self._lock:
            return dict(self._latest_report)


class Handler(BaseHTTPRequestHandler):
    service = None

    def _write_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._write_json({
                "status": "ok",
                "service": self.service.config.service_name
            })
            return

        if self.path == "/status":
            self._write_json(self.service.get_report())
            return

        self._write_json({"error": "not_found"}, status=404)

    def do_POST(self):
        if self.path == "/analyze":
            report = self.service.analyze_once()
            self._write_json(report)
            return

        self._write_json({"error": "not_found"}, status=404)

    def log_message(self, format, *args):
        return


def run():
    config = load_config()
    service = AnomalyDetectionService(config)
    Handler.service = service

    server = HTTPServer(("0.0.0.0", config.port), Handler)
    logging.info(json.dumps({
        "event": "service_started",
        "service": config.service_name,
        "port": config.port,
        "telemetry_file": config.telemetry_file,
        "redis_channel": config.redis_channel
    }))
    server.serve_forever()


if __name__ == "__main__":
    run()
