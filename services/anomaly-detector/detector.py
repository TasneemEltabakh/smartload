import math
from collections import defaultdict
from datetime import datetime, timezone


class AnomalyDetector:
    def __init__(self, config):
        self.config = config

    def evaluate(self, records):
        if not records:
            return []

        grouped = defaultdict(list)
        for record in records:
            grouped[record["node_id"]].append(record)

        reference_now = max(self._parse_timestamp(record["timestamp"]) for record in records)
        results = []
        for node_id, node_records in grouped.items():
            ordered = sorted(node_records, key=lambda item: item["timestamp"])
            results.append(self._evaluate_node(node_id, ordered, reference_now))
        return sorted(results, key=lambda item: item["node_id"])

    def _evaluate_node(self, node_id, records, reference_now):
        latest = records[-1]
        latest_time = self._parse_timestamp(latest["timestamp"])
        age_seconds = (reference_now - latest_time).total_seconds()

        latencies = [float(record.get("latency_ms", 0.0)) for record in records]
        previous_latencies = latencies[:-1]
        latest_latency = latencies[-1]
        error_rate = float(latest.get("error_rate", 0.0))

        baseline = self._ewma(previous_latencies) if previous_latencies else latest_latency
        z_score = self._z_score(previous_latencies, latest_latency)
        history_size = len(previous_latencies)

        if age_seconds > self.config.stale_after_seconds:
            return self._result(
                latest,
                "unhealthy",
                "telemetry stream is stale",
                {
                    "seconds_since_last_metric": age_seconds,
                    "stale_after_seconds": self.config.stale_after_seconds
                }
            )

        if error_rate >= self.config.critical_error_rate:
            return self._result(
                latest,
                "unhealthy",
                "error rate exceeded critical threshold",
                {
                    "error_rate": error_rate,
                    "critical_error_rate": self.config.critical_error_rate
                }
            )

        if (
            history_size >= self.config.minimum_history_points
            and latest_latency >= self.config.critical_latency_floor_ms
            and z_score >= self.config.critical_z_score
        ):
            return self._result(
                latest,
                "unhealthy",
                "latency anomaly exceeded critical threshold",
                {
                    "latency_ms": latest_latency,
                    "latency_baseline_ms": baseline,
                    "z_score": z_score
                }
            )

        if error_rate >= self.config.warning_error_rate:
            return self._result(
                latest,
                "degraded",
                "error rate exceeded warning threshold",
                {
                    "error_rate": error_rate,
                    "warning_error_rate": self.config.warning_error_rate
                }
            )

        if (
            history_size >= self.config.minimum_history_points
            and latest_latency >= self.config.warning_latency_floor_ms
            and z_score >= self.config.warning_z_score
        ):
            return self._result(
                latest,
                "degraded",
                "latency anomaly exceeded warning threshold",
                {
                    "latency_ms": latest_latency,
                    "latency_baseline_ms": baseline,
                    "z_score": z_score
                }
            )

        return self._result(
            latest,
            "healthy",
            "metrics are within expected bounds",
            {
                "latency_ms": latest_latency,
                "latency_baseline_ms": baseline,
                "error_rate": error_rate,
                "history_points": history_size
            }
        )

    def _result(self, latest_record, state, reason, metrics):
        return {
            "timestamp": latest_record["timestamp"],
            "node_id": latest_record["node_id"],
            "service_name": latest_record["service_name"],
            "state": state,
            "reason": reason,
            "trigger_metrics": metrics
        }

    def _parse_timestamp(self, value):
        if value.endswith("Z"):
            value = value.replace("Z", "+00:00")
        return datetime.fromisoformat(value).astimezone(timezone.utc)

    def _ewma(self, values):
        if not values:
            return 0.0
        baseline = values[0]
        for value in values[1:]:
            baseline = (self.config.ewma_alpha * value) + ((1 - self.config.ewma_alpha) * baseline)
        return baseline

    def _z_score(self, values, latest_value):
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        std_dev = math.sqrt(variance)
        if std_dev == 0:
            return 0.0 if latest_value <= mean else float("inf")
        return (latest_value - mean) / std_dev
