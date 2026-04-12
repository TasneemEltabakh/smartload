import sys
import unittest
from pathlib import Path


SERVICE_DIR = Path(__file__).resolve().parents[2] / "services" / "rl-engine"
sys.path.insert(0, str(SERVICE_DIR))

from config import load_config  # noqa: E402
from policy import RoutingScorePolicy  # noqa: E402


class RLEngineTests(unittest.TestCase):
    def setUp(self):
        self.policy = RoutingScorePolicy(load_config())

    def test_healthier_backend_gets_higher_score(self):
        records = [
            self._record("backend-1", 95, 0.01, 95, 0.42, 0.48),
            self._record("backend-2", 520, 0.24, 135, 0.85, 0.79),
        ]
        decision = self.policy.score(records)
        self.assertEqual(decision["ranked_nodes"][0], "backend-1")
        self.assertEqual(decision["ranked_nodes"][-1], "backend-2")

    def test_unhealthy_like_backend_is_heavily_penalized(self):
        records = [
            self._record("backend-1", 100, 0.01, 90, 0.40, 0.45),
            self._record("backend-2", 130, 0.21, 92, 0.41, 0.46),
        ]
        scores = {item["node_id"]: item["score"] for item in self.policy.score(records)["scores"]}
        self.assertGreater(scores["backend-1"], scores["backend-2"])
        self.assertLess(scores["backend-2"], 0.5)

    def test_multiple_backends_are_ranked_descending(self):
        records = [
            self._record("backend-1", 100, 0.01, 100, 0.40, 0.45),
            self._record("backend-2", 150, 0.02, 110, 0.50, 0.55),
            self._record("backend-3", 220, 0.05, 120, 0.60, 0.60),
        ]
        decision = self.policy.score(records)
        scores = [item["score"] for item in decision["scores"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def _record(self, node_id, latency_ms, error_rate, request_count, cpu_usage, memory_usage):
        return {
            "timestamp": "2026-04-12T20:00:00Z",
            "node_id": node_id,
            "latency_ms": latency_ms,
            "error_rate": error_rate,
            "request_count": request_count,
            "cpu_usage": cpu_usage,
            "memory_usage": memory_usage
        }


if __name__ == "__main__":
    unittest.main()
