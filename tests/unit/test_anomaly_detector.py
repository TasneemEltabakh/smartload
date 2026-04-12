import sys
import unittest
from pathlib import Path


SERVICE_DIR = Path(__file__).resolve().parents[2] / "services" / "anomaly-detector"
sys.path.insert(0, str(SERVICE_DIR))

from config import load_config  # noqa: E402
from detector import AnomalyDetector  # noqa: E402


class AnomalyDetectorTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.detector = AnomalyDetector(self.config)

    def test_normal_metrics_report_healthy(self):
        records = []
        for minute, latency in enumerate([90, 92, 91, 89, 90, 93]):
            records.append({
                "timestamp": f"2026-04-12T18:0{minute}:00Z",
                "node_id": "backend-healthy",
                "service_name": "test-server",
                "request_count": 100,
                "latency_ms": latency,
                "error_rate": 0.01,
                "cpu_usage": 0.5,
                "memory_usage": 0.55
            })

        result = self.detector.evaluate(records)
        self.assertEqual(result[0]["state"], "healthy")

    def test_error_spike_reports_unhealthy(self):
        records = []
        for minute, error_rate in enumerate([0.01, 0.01, 0.02, 0.02, 0.01, 0.25]):
            records.append({
                "timestamp": f"2026-04-12T18:0{minute}:00Z",
                "node_id": "backend-error",
                "service_name": "test-server",
                "request_count": 100,
                "latency_ms": 95,
                "error_rate": error_rate,
                "cpu_usage": 0.5,
                "memory_usage": 0.55
            })

        result = self.detector.evaluate(records)
        self.assertEqual(result[0]["state"], "unhealthy")
        self.assertIn("error rate", result[0]["reason"])

    def test_latency_spike_reports_degraded_or_unhealthy(self):
        records = []
        for minute, latency in enumerate([95, 96, 94, 97, 95, 520]):
            records.append({
                "timestamp": f"2026-04-12T18:0{minute}:00Z",
                "node_id": "backend-latency",
                "service_name": "test-server",
                "request_count": 100,
                "latency_ms": latency,
                "error_rate": 0.01,
                "cpu_usage": 0.5,
                "memory_usage": 0.55
            })

        result = self.detector.evaluate(records)
        self.assertIn(result[0]["state"], {"degraded", "unhealthy"})
        self.assertIn("latency anomaly", result[0]["reason"])

    def test_stale_metrics_report_unhealthy(self):
        records = []
        timestamps = [
            "2026-04-12T18:00:00Z",
            "2026-04-12T18:01:00Z",
            "2026-04-12T18:02:00Z",
            "2026-04-12T18:03:00Z",
            "2026-04-12T18:04:00Z",
            "2026-04-12T18:10:00Z"
        ]
        for index, timestamp in enumerate(timestamps):
            records.append({
                "timestamp": timestamp,
                "node_id": "backend-stale" if index < 5 else "backend-fresh",
                "service_name": "test-server",
                "request_count": 100,
                "latency_ms": 95,
                "error_rate": 0.01,
                "cpu_usage": 0.5,
                "memory_usage": 0.55
            })

        results = {item["node_id"]: item for item in self.detector.evaluate(records)}
        self.assertEqual(results["backend-stale"]["state"], "unhealthy")
        self.assertEqual(results["backend-fresh"]["state"], "healthy")


if __name__ == "__main__":
    unittest.main()
