from collections import defaultdict


class RoutingScorePolicy:
    def __init__(self, config):
        self.config = config

    def score(self, records):
        grouped = defaultdict(list)
        for record in records:
            grouped[record["node_id"]].append(record)

        latest_records = []
        for node_id, node_records in grouped.items():
            latest_records.append(sorted(node_records, key=lambda item: item["timestamp"])[-1])

        if not latest_records:
            return {
                "generated_at": None,
                "policy_version": "rl-engine-v1",
                "scores": [],
                "ranked_nodes": []
            }

        max_request_count = max(record["request_count"] for record in latest_records) or 1.0
        scores = []
        for record in latest_records:
            score = self._score_record(record, max_request_count)
            scores.append({
                "node_id": record["node_id"],
                "score": round(score, 4),
                "reason": self._reason_summary(record)
            })

        scores.sort(key=lambda item: item["score"], reverse=True)
        return {
            "generated_at": latest_records[0]["timestamp"],
            "policy_version": "rl-engine-v1",
            "scores": scores,
            "ranked_nodes": [item["node_id"] for item in scores]
        }

    def _score_record(self, record, max_request_count):
        latency_ratio = min(record["latency_ms"] / self.config.max_latency_ms, 1.0)
        error_ratio = min(record["error_rate"], 1.0)
        cpu_ratio = min(record["cpu_usage"], 1.0)
        memory_ratio = min(record["memory_usage"], 1.0)
        load_ratio = min(record["request_count"] / max_request_count, 1.0)

        penalty = (
            (latency_ratio * self.config.latency_weight)
            + (error_ratio * self.config.error_weight)
            + (cpu_ratio * self.config.cpu_weight)
            + (memory_ratio * self.config.memory_weight)
            + (load_ratio * self.config.load_weight)
        )

        if error_ratio >= self.config.anomaly_penalty_threshold:
            penalty += 0.20
        if error_ratio >= 0.20:
            penalty += 0.30

        return max(0.0, 1.0 - penalty)

    def _reason_summary(self, record):
        return {
            "latency_ms": record["latency_ms"],
            "error_rate": record["error_rate"],
            "cpu_usage": record["cpu_usage"],
            "memory_usage": record["memory_usage"],
            "request_count": record["request_count"]
        }
