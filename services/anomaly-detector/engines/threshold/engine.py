"""Threshold-based anomaly engine. Baseline that ships before the trained model."""

from __future__ import annotations

import sys
from pathlib import Path

# Make the service root importable so we can find engine_base.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from engine_base import AnomalyEngine, AnomalyScore, BackendFeatures  # noqa: E402


class ThresholdEngine(AnomalyEngine):
    """Flag a backend as degraded / unhealthy based on simple rules:

      - latency_ms > latency_multiplier × latency_rolling_mean_ms → degraded
      - error_rate > error_rate_threshold                          → unhealthy

    No training, no model file. This engine is the safety net: if the
    trained engine fails to load, the service falls back here.
    """

    def __init__(
        self,
        latency_multiplier: float = 3.0,
        error_rate_threshold: float = 0.05,
        min_sample_count: int = 10,
    ):
        self.latency_multiplier = latency_multiplier
        self.error_rate_threshold = error_rate_threshold
        self.min_sample_count = min_sample_count

    def score(self, features: BackendFeatures) -> AnomalyScore:
        if features.sample_count < self.min_sample_count:
            return AnomalyScore(features.backend_id, "healthy", 0.0)

        if features.error_rate > self.error_rate_threshold:
            return AnomalyScore(
                features.backend_id,
                "unhealthy",
                min(1.0, features.error_rate / self.error_rate_threshold),
                metric="error_rate",
                observed_value=features.error_rate,
                threshold=self.error_rate_threshold,
            )

        if features.latency_rolling_mean_ms <= 0:
            return AnomalyScore(features.backend_id, "healthy", 0.0)

        ratio = features.latency_ms / features.latency_rolling_mean_ms
        if ratio > self.latency_multiplier:
            return AnomalyScore(
                features.backend_id,
                "degraded",
                min(1.0, ratio / (self.latency_multiplier * 2)),
                metric="latency_ms",
                observed_value=features.latency_ms,
                threshold=self.latency_multiplier * features.latency_rolling_mean_ms,
            )

        return AnomalyScore(features.backend_id, "healthy", 0.0)
