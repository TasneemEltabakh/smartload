import json


class JsonForecastRepository:
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
            "request_count": float(record["request_count"])
        }
