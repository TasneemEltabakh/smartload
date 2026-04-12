import json


class JsonTelemetryRepository:
    def __init__(self, file_path):
        self.file_path = file_path

    def fetch_all(self):
        with open(self.file_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)

        records = payload["records"] if isinstance(payload, dict) else payload
        return [self._normalize(record) for record in records]

    def _normalize(self, record):
        return {
            "timestamp": record["timestamp"],
            "node_id": record["node_id"],
            "service_name": record.get("service_name", "backend"),
            "request_count": int(record.get("request_count", 0)),
            "latency_ms": float(record.get("latency_ms", 0.0)),
            "error_rate": float(record.get("error_rate", 0.0)),
            "cpu_usage": float(record.get("cpu_usage", 0.0)),
            "memory_usage": float(record.get("memory_usage", 0.0))
        }
