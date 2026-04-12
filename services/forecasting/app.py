import json
import logging
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from config import load_config
from forecaster import ExponentialSmoothingForecaster
from publisher import RedisForecastPublisher
from repository import JsonForecastRepository


logging.basicConfig(level=logging.INFO, format="%(message)s")


class ForecastingService:
    def __init__(self, config):
        self.config = config
        self.repository = JsonForecastRepository(config.history_file)
        self.forecaster = ExponentialSmoothingForecaster(config)
        self.publisher = RedisForecastPublisher(config)
        self._lock = threading.Lock()
        self._latest_report = {
            "service": config.service_name,
            "last_run_at": None,
            "forecast": None,
            "evaluation": None
        }

    def forecast_once(self):
        records = self.repository.fetch_all()
        forecast_report = self.forecaster.forecast(records)
        evaluation_report = self.forecaster.evaluate(records)

        payload = {
            "service": self.config.service_name,
            "last_run_at": datetime.now(timezone.utc).isoformat(),
            "forecast": forecast_report,
            "evaluation": evaluation_report
        }

        self.publisher.publish(payload)

        logging.info(json.dumps({
            "event": "forecast_generated",
            "timestamp": payload["last_run_at"],
            "predicted_request_rate": forecast_report["predicted_request_rate"],
            "horizon_minutes": forecast_report["horizon_minutes"],
            "model": forecast_report["model"],
            "mape": evaluation_report["mape"]
        }))

        with self._lock:
            self._latest_report = payload
        return payload

    def get_report(self):
        with self._lock:
            return dict(self._latest_report)


class ForecastScheduler(threading.Thread):
    def __init__(self, service):
        super().__init__(daemon=True)
        self.service = service

    def run(self):
        while True:
            try:
                self.service.forecast_once()
            except Exception as exc:  # pragma: no cover - runtime resilience
                logging.warning(json.dumps({
                    "event": "forecast_cycle_failed",
                    "error": str(exc)
                }))
            time.sleep(self.service.config.forecast_interval_seconds)


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
        if self.path == "/forecast":
            self._write_json(self.service.forecast_once())
            return

        self._write_json({"error": "not_found"}, status=404)

    def log_message(self, format, *args):
        return


def run():
    config = load_config()
    service = ForecastingService(config)
    Handler.service = service

    if config.enable_scheduler:
        ForecastScheduler(service).start()

    server = HTTPServer(("0.0.0.0", config.port), Handler)
    logging.info(json.dumps({
        "event": "service_started",
        "service": config.service_name,
        "port": config.port,
        "history_file": config.history_file,
        "redis_channel": config.redis_channel,
        "scheduler_enabled": config.enable_scheduler,
        "forecast_interval_seconds": config.forecast_interval_seconds
    }))
    server.serve_forever()


if __name__ == "__main__":
    run()
