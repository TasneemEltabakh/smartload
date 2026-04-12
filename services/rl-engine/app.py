import json
import logging
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from config import load_config
from policy import RoutingScorePolicy
from publisher import RedisRoutingPublisher
from repository import JsonTelemetryRepository


logging.basicConfig(level=logging.INFO, format="%(message)s")


class RLEngineService:
    def __init__(self, config):
        self.config = config
        self.repository = JsonTelemetryRepository(config.telemetry_file)
        self.policy = RoutingScorePolicy(config)
        self.publisher = RedisRoutingPublisher(config)
        self._latest_report = {
            "service": config.service_name,
            "last_run_at": None,
            "decision": None
        }

    def score_once(self):
        records = self.repository.fetch_all()
        decision = self.policy.score(records)
        payload = {
            "service": self.config.service_name,
            "last_run_at": datetime.now(timezone.utc).isoformat(),
            "decision": decision
        }

        self.publisher.publish(payload)
        logging.info(json.dumps({
            "event": "routing_scores_generated",
            "timestamp": payload["last_run_at"],
            "policy_version": decision["policy_version"],
            "top_node": decision["ranked_nodes"][0] if decision["ranked_nodes"] else None
        }))

        self._latest_report = payload
        return payload

    def get_report(self):
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
        if self.path == "/score":
            self._write_json(self.service.score_once())
            return

        self._write_json({"error": "not_found"}, status=404)

    def log_message(self, format, *args):
        return


def run():
    config = load_config()
    service = RLEngineService(config)
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
